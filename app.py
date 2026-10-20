import os
import io
import json
import pandas as pd
import time
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, redirect, session, abort

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from supabase import create_client
from datetime import datetime, timezone
import requests
import base64
import re

load_dotenv()

app = Flask(__name__)

app.secret_key = os.getenv("FLASK_SECRET_KEY", "refracta-development-key")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

GEMINI_KEYS = [
    key for key in (
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY2"),
        os.getenv("GEMINI_API_KEY3")
    ) if key
]

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Cadangan jika gagal mengambil daftar model dari OpenRouter.
# Nama model gratis sering berubah. Cek daftar terbaru di:
# https://openrouter.ai/models?input_modalities=image&max_price=0
OPENROUTER_FALLBACK_MODELS = [
    "openrouter/free"
]

# Maksimal model OpenRouter yang dicoba per request (agar tetap cepat)
OPENROUTER_MAX_TRIES = 4

_free_vision_cache = {"models": [], "ts": 0}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def openrouter_headers():
    return {
        "Authorization": f"Bearer {OPENROUTER_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "Refracta"
    }


def login_required(view):
    """Wajib login. Untuk /api/... balas JSON 401, selain itu ke /login."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "error": "Unauthorized."
                }), 401
            return redirect("/login")
        return view(*args, **kwargs)
    return wrapper


class Transaction(BaseModel):
    description: str = Field(
        description="Short and clear title or description of the transaction."
    )
    category: str = Field(
        description="Transaction category. Use 'Other' if the category cannot be determined."
    )


class ReceiptData(BaseModel):
    transactions: list[Transaction]


def clean_ai_json(raw_text):
    if not raw_text:
        raise Exception("AI returned empty content.")

    if isinstance(raw_text, list):
        raw_text = "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in raw_text
        )

    raw_text = str(raw_text).strip()

    # Hapus pagar Markdown seperti ```json ... ```
    raw_text = re.sub(r"```(?:json)?", "", raw_text, flags=re.IGNORECASE)

    # Cari objek JSON valid yang memiliki key "transactions".
    # Lebih aman daripada find("{") ssampai rfind("}"), karena model
    # kadang menulis teks panjang yang juga mengandung kurung kurawal.
    decoder = json.JSONDecoder()
    candidate = None

    for match in re.finditer(r"\{", raw_text):
        try:
            obj, _ = decoder.raw_decode(raw_text[match.start():])
        except json.JSONDecodeError:
            continue

        if isinstance(obj, dict) and "transactions" in obj:
            # Ambil yang terakhir (biasanya jawaban final)
            candidate = obj

    if candidate is None:
        raise Exception(
            f"AI did not return valid JSON. Response: {raw_text[:300]}"
        )

    return candidate


def _is_zero(value):
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def _is_reasoning_model(model_id):
    low = model_id.lower()
    return any(k in low for k in ("reason", "think", "r1"))


def get_free_vision_models():
    """
    Ambil model OpenRouter yang: gratis, menerima gambar, dan keluarannya teks.
    Hasil di-cache 1 jam supaya tidak memperlambat setiap request.
    """
    now = time.time()

    if _free_vision_cache["models"] and now - _free_vision_cache["ts"] < 3600:
        return _free_vision_cache["models"]

    try:
        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            timeout=15
        )
        response.raise_for_status()

        found = []

        for model in response.json().get("data", []):
            architecture = model.get("architecture") or {}
            inputs = architecture.get("input_modalities") or []
            outputs = architecture.get("output_modalities") or []
            pricing = model.get("pricing") or {}

            if (
                "image" in inputs
                and outputs == ["text"]
                and _is_zero(pricing.get("prompt"))
                and _is_zero(pricing.get("completion"))
            ):
                found.append(model["id"])

        # Model non-reasoning didahulukan: lebih cepat dan langsung JSON.
        found.sort(key=_is_reasoning_model)
        found = found[:OPENROUTER_MAX_TRIES]

        if found:
            _free_vision_cache["models"] = found
            _free_vision_cache["ts"] = now
            print("Free vision models on OpenRouter:", found)
            return found

    except Exception as e:
        print("Failed to fetch OpenRouter model list:")
        print(e)

    return OPENROUTER_FALLBACK_MODELS


def prepare_image_for_ai(image_bytes, mime_type, max_side=1600):
    """
    Perkecil gambar besar sebelum dikirim ke OpenRouter
    (lebih cepat, hemat token). Jika Pillow tidak terpasang atau
    file bukan gambar, file asli dikembalikan apa adanya.
    Install (opsional): pip install pillow
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return image_bytes, mime_type

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)

        if max(img.size) <= max_side and len(image_bytes) < 1_500_000:
            return image_bytes, mime_type

        img = img.convert("RGB")
        img.thumbnail((max_side, max_side))

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85, optimize=True)

        return buffer.getvalue(), "image/jpeg"

    except Exception as e:
        print("Image resize skipped:")
        print(e)
        return image_bytes, mime_type


def extract_receipt(image_bytes, mime_type):
    prompt = """
    Analyze the provided receipt, invoice, bookkeeping page, or financial
    transaction image.

    Extract every identifiable transaction.

    For each transaction:
    - Create a short and clear description/title.
    - Assign a reasonable category based on the transaction.
    - Do not invent information.
    - Do not create details that are not present in the document.
    - If the category cannot be determined with reasonable confidence,
      use "Other".
    - Keep descriptions concise but meaningful.
    - Carefully read both printed and handwritten bookkeeping.

    IMPORTANT:
    Return ONLY valid JSON.
    Do not return Markdown.
    Do not return ```json.
    Do not add explanations before or after the JSON.
    Do not describe your reasoning or analysis.

    The JSON must have exactly this structure:
    {
        "transactions": [
            {
                "description": "string",
                "category": "string"
            }
        ]
    }

    Do NOT include date, quantity, unit_price, total_price, amount,
    price, or any other fields.
    """

    last_error = None

    # =========================================================
    # 1. GEMINI
    # =========================================================

    for index, key in enumerate(GEMINI_KEYS, start=1):
        try:
            print(
                f"Trying Gemini API key "
                f"{index}/{len(GEMINI_KEYS)}..."
            )

            client = genai.Client(api_key=key)

            image_part = types.Part.from_bytes(
                data=image_bytes,
                mime_type=mime_type
            )

            response = None

            # Retry singkat untuk error sementara (503 high demand / 429)
            for attempt in range(3):
                try:
                    response = client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=[
                            prompt,
                            image_part
                        ],
                        config={
                            "response_mime_type": "application/json",
                            "response_schema": ReceiptData
                        }
                    )
                    break

                except Exception as e:
                    message = str(e)
                    temporary = (
                        "503" in message
                        or "UNAVAILABLE" in message
                        or "429" in message
                    )

                    if temporary and attempt < 2:
                        wait = 2 * (attempt + 1)
                        print(
                            f"Gemini temporary error, retrying "
                            f"in {wait}s (attempt {attempt + 1}/3)..."
                        )
                        time.sleep(wait)
                        continue

                    raise

            raw_text = response.text

            if not raw_text:
                raise Exception("returned an empty response.")

            result = json.loads(raw_text)
            validated = ReceiptData.model_validate(result)

            print(f"Gemini API key {index} succeeded.")
            return validated.model_dump()

        except Exception as e:
            last_error = str(e)
            print(f"Gemini API key {index} failed:")
            print(e)

    print("All Gemini API keys failed.")

    # =========================================================
    # 2. OPENROUTER FALLBACK (hanya model gratis yang menerima gambar)
    # =========================================================

    if not OPENROUTER_KEY:
        raise Exception(
            "All providers failed and is not configured."
        )

    ai_bytes, ai_mime = prepare_image_for_ai(image_bytes, mime_type)

    image_base64 = base64.b64encode(ai_bytes).decode("utf-8")
    image_data_url = f"data:{ai_mime};base64,{image_base64}"

    headers = openrouter_headers()

    # "openrouter/free" = router resmi OpenRouter yang memilih model gratis
    # secara acak dan otomatis menyaring yang mendukung input gambar.
    # Dicoba dulu; jika gagal, lanjut ke daftar model gratis berbasis gambar.
    models_to_try = ["openrouter/free"]

    for free_model in get_free_vision_models():
        if free_model not in models_to_try:
            models_to_try.append(free_model)

    for model_index, model in enumerate(models_to_try, start=1):
        try:
            print(
                f"Trying OpenRouter model "
                f"{model_index}/{len(models_to_try)}: {model}"
            )

            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_data_url
                                }
                            }
                        ]
                    }
                ],
                "temperature": 0,
                "max_tokens": 4000,
                "response_format": {"type": "json_object"},
                "reasoning": {"exclude": True}
            }

            response = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=90
            )

            if response.status_code != 200:
                raise Exception(
                    f"OpenRouter error {response.status_code}: "
                    f"{response.text[:500]}"
                )

            response_data = response.json()
            choices = response_data.get("choices")

            if not choices:
                raise Exception("OpenRouter returned no choices.")

            message = choices[0].get("message", {})
            raw_text = message.get("content")

            if not raw_text:
                raise Exception(
                    "OpenRouter returned empty content."
                )

            result = clean_ai_json(raw_text)
            validated = ReceiptData.model_validate(result)

            print(f"OpenRouter model succeeded: {model}")
            return validated.model_dump()

        except Exception as e:
            last_error = str(e)
            print(f"OpenRouter model failed: {model}")
            print(e)
            continue

    raise Exception(
        "All AI providers failed. "
        f"Last error: {last_error}"
    )

def _csv_safe(value):
    value = "" if value is None else str(value)

    # Cegah CSV injection saat dibuka di Excel
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value

    return value


def make_csv_name(original_name):
    base_name = os.path.splitext(str(original_name or ""))[0]
    base_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base_name)
    base_name = base_name.strip(" .") or "refracta_transactions"
    return f"{base_name}.csv"


def build_csv_bytes(transactions):
    df = pd.DataFrame(
        [
            {
                "Description": _csv_safe(t.get("description", "")),
                "Category": _csv_safe(t.get("category", "Other"))
            }
            for t in transactions
        ],
        columns=["Description", "Category"]
    )

    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)

    return csv_buffer.getvalue().encode("utf-8-sig")


@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route("/scan")
@login_required
def scan_page():
    return render_template("scan.html")

@app.route("/api/upload-url", methods=["POST"])
@login_required
def create_upload_url():

    data = request.get_json(silent=True) or {}

    filename = data.get("filename", "")
    mime_type = data.get("mime_type", "")

    if not filename:
        return jsonify({
            "success": False,
            "error": "Filename is required."
        }), 400

    user_id = session["user_id"]

    extension = os.path.splitext(filename)[1].lower()

    if not extension:
        extension = ".jpg"

    storage_path = (
        f"{user_id}/receipts/"
        f"receipt_{os.urandom(8).hex()}{extension}"
    )

    try:

        response = supabase.storage \
            .from_("refracta-files") \
            .create_signed_upload_url(storage_path)

        upload_url = response.get("signedURL")

        if not upload_url:
            upload_url = response.get("signed_url")

        if not upload_url:
            raise Exception(
                "Failed to create upload URL."
            )
        print(upload_url)
        return jsonify({
            "success": True,
            "upload_url": upload_url,
            "storage_path": storage_path,
            "mime_type": mime_type
        })

    except Exception as e:

        print("Storage upload URL error:")
        print(e)

        return jsonify({
            "success": False,
            "error": "Failed to prepare file upload."
        }), 500

@app.route("/api/process-upload", methods=["POST"])
@login_required
def process_uploaded_file():

    data = request.get_json(silent=True) or {}
    
    storage_path = data.get("storage_path")
    print(storage_path)
    original_filename = data.get(
        "original_filename",
        "receipt"
    )
    mime_type = data.get(
        "mime_type",
        "image/jpeg"
    )

    if not storage_path:
        return jsonify({
            "success": False,
            "error": "Storage path is required."
        }), 400

    user_id = session["user_id"]

    # Keamanan: pastikan file milik user yang sedang login
    if (
        not storage_path.startswith(f"{user_id}/receipts/")
        or ".." in storage_path
    ):
        return jsonify({
            "success": False,
            "error": "Invalid storage path."
        }), 403

    try:
        image_bytes = None

        for attempt in range(4):
            try:
                print(
                    f"Downloading from Storage "
                    f"(attempt {attempt + 1}/4): {storage_path}"
                )

                image_bytes = supabase.storage \
                    .from_("refracta-files") \
                    .download(storage_path)

                if image_bytes:
                    print("Storage download successful.")
                    break

            except Exception as e:
                print(
                    f"Storage download attempt "
                    f"{attempt + 1} failed:"
                )
                print(e)

                if attempt < 3:
                    time.sleep(1)

        if not image_bytes:
            raise Exception(
                "File was uploaded, but could not be retrieved "
                "from Supabase Storage after 4 attempts."
            )

        result = extract_receipt(
            image_bytes,
            mime_type
        )

        transactions = result.get(
            "transactions",
            []
        )

        # Buat CSV langsung dari hasil AI dan simpan ke Storage
        csv_name = make_csv_name(original_filename)
        csv_path = (
            f"{user_id}/outputs/"
            f"csv_{os.urandom(8).hex()}.csv"
        )

        supabase.storage \
            .from_("refracta-files") \
            .upload(
                csv_path,
                build_csv_bytes(transactions),
                {
                    "content-type": "text/csv",
                    "upsert": "true"
                }
            )

        try:
            file_response = supabase.table("files").insert({
                "user_id": user_id,
                "name": original_filename,
                "file_type": "receipt",
                "mime_type": mime_type,
                "storage_path": storage_path,
                "source": "scanner",
                "csv_path": csv_path,
                "csv_name": csv_name,
                "row_count": len(transactions)
            }).execute()

            if not file_response.data:
                raise Exception(
                    "Failed to save file information."
                )

        except Exception:
            # Hapus CSV yatim jika baris files gagal disimpan
            try:
                supabase.storage \
                    .from_("refracta-files") \
                    .remove([csv_path])
            except Exception:
                pass
            raise

        file_id = file_response.data[0]["id"]


        return jsonify({
            "success": True,
            "data": {
                "file_id": file_id,
                "filename": original_filename,
                "storage_path": storage_path,
                "transactions": transactions
            }
        })

    except Exception as e:

        print("Process upload error:")
        print(e)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

@app.route('/login')
def login_page():
    if "user_id" in session:
        return redirect('/')
    return render_template('login.html')

@app.route('/register')
def register_page():
    if "user_id" in session:
        return redirect('/')
    return render_template('register.html')



@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json()

    full_name = data.get("full_name", "").strip()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not full_name or not email or not password:
        return jsonify({
            "success": False,
            "error": "All fields are required."
        }), 400

    if len(password) < 6:
        return jsonify({
            "success": False,
            "error": "Password must be at least 6 characters."
        }), 400

    try:
        response = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {
                "data": {
                    "full_name": full_name
                }
            }
        })

        if response.user is None:
            return jsonify({
                "success": False,
                "error": "Registration failed."
            }), 400

        if response.session is None:
            return jsonify({
                "success": False,
                "error": "This email is already registered."
            }), 400

        return jsonify({
            "success": True,
            "message": "Registration successful."
        })

    except Exception as e:
        error_message = str(e).lower()

        if "already registered" in error_message:
            return jsonify({
                "success": False,
                "error": "This email is already registered."
            }), 400

        if "invalid email" in error_message:
            return jsonify({
                "success": False,
                "error": "Please enter a valid email address."
            }), 400

        if "password" in error_message:
            return jsonify({
                "success": False,
                "error": "Password does not meet the requirements."
            }), 400

        return jsonify({
            "success": False,
            "error": "Registration failed. Please try again."
        }), 400


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json()

    email = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({
            "success": False,
            "error": "Email and password are required."
        }), 400

    try:
        response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })

        if response.user is None or response.session is None:
            return jsonify({
                "success": False,
                "error": "Invalid email or password."
            }), 401

        session["user_id"] = response.user.id
        session["access_token"] = response.session.access_token
        session["email"] = response.user.email

        return jsonify({
            "success": True,
            "message": "Login successful."
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()

    return jsonify({
        "success": True
    })


@app.route("/api/me")
def get_current_user():
    if "user_id" not in session:
        return jsonify({
            "authenticated": False
        })

    return jsonify({
        "authenticated": True,
        "user_id": session["user_id"],
        "email": session["email"]
    })

# =========================================================
# FILES & HISTORY
# =========================================================

def get_user_files(user_id, limit=None, file_type=None):
    """Daftar file milik user, terbaru lebih dulu."""
    if not user_id:
        return []

    query = (
        supabase.table("files")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
    )

    if file_type:
        query = query.eq("file_type", file_type)

    if limit:
        query = query.limit(limit)

    return query.execute().data or []


def get_user_file_by_id(user_id, file_id):
    """Satu file milik user (None jika tidak ada / bukan miliknya)."""
    if not user_id or not file_id:
        return None

    response = (
        supabase.table("files")
        .select("*")
        .eq("user_id", user_id)
        .eq("id", file_id)
        .limit(1)
        .execute()
    )

    return response.data[0] if response.data else None


def create_file_signed_url(storage_path, expires_in=3600):
    """URL sementara untuk file di Supabase Storage."""
    if not storage_path:
        return None

    result = (
        supabase.storage
        .from_("refracta-files")
        .create_signed_url(storage_path, expires_in)
    )

    return result.get("signedURL") or result.get("signedUrl")


def file_error(status, message):
    """JSON untuk /api/..., halaman error biasa untuk route lainnya."""
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "error": message}), status
    abort(status)


@app.route("/history")
@login_required
def history():
    files = get_user_files(session["user_id"], file_type="receipt")

    return render_template("history.html", files=files)


@app.route("/api/history")
@login_required
def api_history():
    try:
        files = get_user_files(
            session["user_id"],
            limit=100,
            file_type="receipt"
        )

    except Exception as e:
        print("History error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Failed to load history."
        }), 500

    return jsonify({
        "success": True,
        "items": [
            {
                "file_id": row["id"],
                "name": row.get("name"),
                "csv_name": row.get("csv_name"),
                "row_count": row.get("row_count"),
                "created_at": row.get("created_at"),
                "photo_url": f"/api/history/{row['id']}/photo",
                "csv_url": f"/api/download/csv/{row['id']}"
            }
            for row in files
        ]
    })


@app.route("/history/<file_id>/image")
@app.route("/api/history/<file_id>/photo")
@login_required
def history_image(file_id):
    file = get_user_file_by_id(session["user_id"], file_id)

    if not file or not file.get("storage_path"):
        return file_error(404, "File not found.")

    try:
        image_url = create_file_signed_url(file["storage_path"])

    except Exception as e:
        print("History image error:", repr(e))
        return file_error(500, "Failed to load photo.")

    if not image_url:
        return file_error(404, "File not found.")

    return redirect(image_url)


@app.route("/history/<file_id>/download")
@app.route("/api/download/csv/<file_id>")
@login_required
def download_history_csv(file_id):
    file = get_user_file_by_id(session["user_id"], file_id)

    if not file or not file.get("csv_path"):
        return file_error(404, "CSV not found.")

    try:
        csv_bytes = (
            supabase.storage
            .from_("refracta-files")
            .download(file["csv_path"])
        )

        return send_file(
            io.BytesIO(csv_bytes),
            mimetype="text/csv",
            as_attachment=True,
            download_name=file.get("csv_name") or "refracta_transactions.csv"
        )

    except Exception as e:
        print("CSV download error:", repr(e))
        return file_error(500, "Failed to download CSV.")


@app.route("/api/history/<file_id>/details")
@login_required
def history_file_details(file_id):
    file = get_user_file_by_id(session["user_id"], file_id)

    if not file:
        return file_error(404, "File tidak ditemukan.")

    original_url = None
    csv_preview = None
    csv_path = file.get("csv_path")

    try:
        original_url = create_file_signed_url(file.get("storage_path"))
    except Exception as e:
        print("Original file preview error:", repr(e))

    if csv_path:
        try:
            csv_bytes = (
                supabase.storage
                .from_("refracta-files")
                .download(csv_path)
            )

            df = pd.read_csv(
                io.BytesIO(csv_bytes),
                nrows=100,
                dtype=str,
                keep_default_na=False,
                encoding="utf-8-sig"
            )

            csv_preview = {
                "columns": df.columns.tolist(),
                "rows": df.to_dict(orient="records"),
                "truncated": len(df) == 100
            }

        except Exception as e:
            print("CSV preview error:", repr(e))

    return jsonify({
        "name": file.get("name") or "Tanpa nama",
        "original_url": original_url,
        "csv_name": file.get("csv_name") or "Hasil olahan.csv",
        "csv_available": bool(csv_path),
        "csv_preview": csv_preview
    })


@app.route("/api/files", methods=["GET"])
@login_required
def get_files():
    keys = ("id", "name", "file_type", "mime_type", "source", "created_at")

    try:
        files = get_user_files(session["user_id"])

    except Exception as e:
        print("Get files error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Failed to retrieve files."
        }), 500

    return jsonify({
        "success": True,
        "files": [{k: f.get(k) for k in keys} for f in files]
    }), 200


# =========================================================
# CHATBOT
# =========================================================

def get_owned_conversation(conversation_id, user_id):
    """Pastikan percakapan memang milik user yang sedang login."""
    result = (
        supabase.table("conversations")
        .select("*")
        .eq("id", conversation_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )

    return result.data[0] if result.data else None


def generate_chat_reply(messages):
    """Gunakan model OpenRouter yang sudah dikonfigurasi di app.py."""
    if not OPENROUTER_KEY:
        raise Exception("OPENROUTER_API_KEY is not configured.")

    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Refracta AI, a helpful assistant for small "
                    "business owners. Help with bookkeeping, business "
                    "ideas, sales, costs, profits, and practical decisions. "
                    "Automatically follow the user's language. If they "
                    "write in Indonesian, reply in Indonesian. If they "
                    "write in English, reply in English. Follow language "
                    "switches naturally. Be clear and practical. Do not "
                    "invent business figures or claim to access files "
                    "that were not provided."
                )
            },
            *messages
        ],
        "temperature": 0.4,
        "max_tokens": 1200
    }

    headers = openrouter_headers()

    payload["model"] = "openrouter/free"

    try:
        response = requests.post(
            OPENROUTER_URL,
            headers=headers,
            json=payload,
            timeout=60
        )

        if not response.ok:
            print("OpenRouter error:", response.status_code, response.text[:1000])
            raise Exception(
                f"OpenRouter returned HTTP {response.status_code}"
            )

        result = response.json()
        answer = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        if isinstance(answer, list):
            answer = "\n".join(
                part.get("text", "")
                for part in answer
                if isinstance(part, dict)
            )

        if not isinstance(answer, str) or not answer.strip():
            raise Exception("OpenRouter returned an empty response.")

        return answer.strip()

    except requests.Timeout:
        raise Exception("OpenRouter request timed out.")


@app.route("/chatbot")
@login_required
def chatbot_page():
    return render_template("chatbot.html")


@app.route("/api/conversations", methods=["GET"])
@login_required
def list_conversations():
    try:
        result = (
            supabase.table("conversations")
            .select("id, title, created_at, updated_at")
            .eq("user_id", session["user_id"])
            .order("updated_at", desc=True)
            .execute()
        )

        return jsonify({
            "success": True,
            "conversations": result.data or []
        })

    except Exception as e:
        print("List conversations error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Could not load conversation history."
        }), 500


@app.route("/api/conversations", methods=["POST"])
@login_required
def create_conversation():
    try:
        data = request.get_json(silent=True) or {}
        title = str(data.get("title") or "New chat").strip()[:100]

        result = (
            supabase.table("conversations")
            .insert({
                "user_id": session["user_id"],
                "title": title or "New chat"
            })
            .execute()
        )

        if not result.data:
            raise Exception("Conversation was not created.")

        return jsonify({
            "success": True,
            "conversation": result.data[0]
        }), 201

    except Exception as e:
        print("Create conversation error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Could not create a new conversation."
        }), 500


@app.route("/api/conversations/<conversation_id>", methods=["GET"])
@login_required
def get_conversation_messages(conversation_id):
    try:
        conversation = get_owned_conversation(
            conversation_id,
            session["user_id"]
        )

        if not conversation:
            return jsonify({
                "success": False,
                "error": "Conversation not found."
            }), 404

        result = (
            supabase.table("messages")
            .select("id, conversation_id, role, content, created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .execute()
        )

        return jsonify({
            "success": True,
            "conversation": conversation,
            "messages": result.data or []
        })

    except Exception as e:
        print("Load messages error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Could not load this conversation."
        }), 500


@app.route(
    "/api/conversations/<conversation_id>/messages",
    methods=["POST"]
)
@login_required
def send_chat_message(conversation_id):
    data = request.get_json(silent=True) or {}
    content = str(data.get("content") or "").strip()

    if not content:
        return jsonify({
            "success": False,
            "error": "Please enter a message."
        }), 400

    if len(content) > 8000:
        return jsonify({
            "success": False,
            "error": "Message is too long. Maximum 8000 characters."
        }), 400

    user_id = session["user_id"]

    try:
        conversation = get_owned_conversation(conversation_id, user_id)

        if not conversation:
            return jsonify({
                "success": False,
                "error": "Conversation not found."
            }), 404

        # Simpan pesan user terlebih dahulu.
        saved_user = (
            supabase.table("messages")
            .insert({
                "conversation_id": conversation_id,
                "role": "user",
                "content": content
            })
            .execute()
        )

        if not saved_user.data:
            raise Exception("Could not save user message.")

        # Ambil riwayat yang tersimpan untuk konteks AI.
        history_result = (
            supabase.table("messages")
            .select("role, content")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .execute()
        )

        full_history = history_result.data or []

        # Semua pesan tetap tersimpan di database.
        # Untuk konteks model, gunakan pesan terbaru agar request tidak
        # membengkak tanpa batas. Pastikan pesan user terbaru ikut terkirim.
        ai_messages = [
            {
                "role": item["role"],
                "content": item["content"]
            }
            for item in full_history[-20:]
            if item.get("role") in ("user", "assistant")
            and isinstance(item.get("content"), str)
        ]

        try:
            answer = generate_chat_reply(ai_messages)
        except Exception as ai_error:
            print("OpenRouter chatbot error:", repr(ai_error))
            return jsonify({
                "success": False,
                "user_message_saved": True,
                "error": (
                    "Your message was saved, but the AI could not respond. "
                    "Please try sending another message."
                )
            }), 502

        # Simpan jawaban AI agar percakapan bisa dibuka kembali.
        saved_assistant = (
            supabase.table("messages")
            .insert({
                "conversation_id": conversation_id,
                "role": "assistant",
                "content": answer
            })
            .execute()
        )

        # Judul otomatis menggunakan pesan pertama, tanpa menimpa judul
        # jika pengguna sudah menggantinya.
        title = conversation.get("title") or "New chat"
        update_data = {"updated_at": now_iso()}

        if title == "New chat":
            update_data["title"] = content[:45] + (
                "..." if len(content) > 45 else ""
            )

        (
            supabase.table("conversations")
            .update(update_data)
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .execute()
        )

        return jsonify({
            "success": True,
            "user_message": saved_user.data[0],
            "assistant_message": (
                saved_assistant.data[0] if saved_assistant.data else {
                    "role": "assistant",
                    "content": answer
                }
            ),
            "title": update_data.get("title", title)
        })

    except Exception as e:
        print("Send chat message error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Could not save or process this message."
        }), 500


@app.route(
    "/api/conversations/<conversation_id>",
    methods=["PATCH"]
)
@login_required
def rename_conversation(conversation_id):
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()[:100]

    if not title:
        return jsonify({
            "success": False,
            "error": "Title cannot be empty."
        }), 400

    try:
        conversation = get_owned_conversation(
            conversation_id,
            session["user_id"]
        )

        if not conversation:
            return jsonify({
                "success": False,
                "error": "Conversation not found."
            }), 404

        supabase.table("conversations").update({
            "title": title,
            "updated_at": now_iso()
        }).eq("id", conversation_id).eq(
            "user_id", session["user_id"]
        ).execute()

        return jsonify({"success": True, "title": title})

    except Exception as e:
        print("Rename conversation error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Could not rename this conversation."
        }), 500


@app.route("/api/conversations/<conversation_id>",methods=["DELETE"])
@login_required
def delete_conversation(conversation_id):
    try:
        conversation = get_owned_conversation(
            conversation_id,
            session["user_id"]
        )

        if not conversation:
            return jsonify({
                "success": False,
                "error": "Conversation not found."
            }), 404

        supabase.table("messages").delete().eq(
            "conversation_id", conversation_id
        ).execute()

        supabase.table("conversations").delete().eq(
            "id", conversation_id
        ).eq("user_id", session["user_id"]).execute()

        return jsonify({"success": True})

    except Exception as e:
        print("Delete conversation error:", repr(e))
        return jsonify({
            "success": False,
            "error": "Could not delete this conversation."
        }), 500

from dashboard_routes import register_dashboard
register_dashboard(app, supabase, login_required)

if __name__ == "__main__":
    app.run(
        debug=True,
        host="0.0.0.0",
        port=5000
    )
