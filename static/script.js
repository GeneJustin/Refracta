const homeSection = document.getElementById("homeSection");
const cameraSection = document.getElementById("cameraSection");
const previewSection = document.getElementById("previewSection");
const resultSection = document.getElementById("resultSection");

const uploadButton = document.getElementById("uploadButton");
const uploadInput = document.getElementById("uploadInput");

const scanButton = document.getElementById("scanButton");
const closeCamera = document.getElementById("closeCamera");

const camera = document.getElementById("camera");
const captureButton = document.getElementById("captureButton");

const previewImage = document.getElementById("previewImage");

const retakeButton = document.getElementById("retakeButton");
const processButton = document.getElementById("processButton");

const backPreview = document.getElementById("backPreview");
const backResult = document.getElementById("backResult");

const resultTable = document.getElementById("resultTable");

const loading = document.getElementById("loading");
const resultContent = document.getElementById("resultContent");

const csvButton = document.getElementById("csvButton");

let stream = null;
let selectedFile = null;
let extractedData = null;
let uploadedStoragePath = null;
let currentFileId = null;


function showSection(section) {
    homeSection.classList.add("hidden");
    cameraSection.classList.add("hidden");
    previewSection.classList.add("hidden");
    resultSection.classList.add("hidden");

    section.classList.remove("hidden");
}


function resetScanner() {
    stopCamera();

    selectedFile = null;
    extractedData = null;
    uploadedStoragePath = null;
    currentFileId = null;

    uploadInput.value = "";

    previewImage.removeAttribute("src");

    resultTable.innerHTML = "";

    loading.classList.add("hidden");
    resultContent.classList.add("hidden");

    showSection(homeSection);
}


uploadButton.addEventListener("click", () => {
    uploadInput.click();
});


uploadInput.addEventListener("change", () => {

    const file = uploadInput.files[0];

    if (!file) {
        return;
    }

    if (!file.type.startsWith("image/")) {
        alert("Please select an image file.");
        uploadInput.value = "";
        return;
    }

    selectedFile = file;
    uploadedStoragePath = null;
    extractedData = null;
    currentFileId = null;

    const url = URL.createObjectURL(file);

    previewImage.src = url;

    showSection(previewSection);
});


scanButton.addEventListener("click", async () => {

    showSection(cameraSection);

    try {

        stream = await navigator.mediaDevices.getUserMedia({
            video: {
                facingMode: {
                    ideal: "environment"
                }
            },
            audio: false
        });

        camera.srcObject = stream;

    } catch (error) {

        console.error("Camera error:", error);

        alert(
            "Camera could not be opened. Please allow camera permission."
        );

        showSection(homeSection);
    }
});


captureButton.addEventListener("click", () => {

    if (!camera.videoWidth || !camera.videoHeight) {
        alert("Camera is not ready yet.");
        return;
    }

    const canvas = document.createElement("canvas");

    canvas.width = camera.videoWidth;
    canvas.height = camera.videoHeight;

    const context = canvas.getContext("2d");

    context.drawImage(
        camera,
        0,
        0,
        canvas.width,
        canvas.height
    );

    canvas.toBlob(
        (blob) => {

            if (!blob) {
                alert("Failed to capture image.");
                return;
            }

            selectedFile = new File(
                [blob],
                "camera_receipt.jpg",
                {
                    type: "image/jpeg"
                }
            );

            uploadedStoragePath = null;
            extractedData = null;
            currentFileId = null;

            previewImage.src = URL.createObjectURL(blob);

            stopCamera();

            showSection(previewSection);

        },
        "image/jpeg",
        0.9
    );
});


function stopCamera() {

    if (!stream) {
        return;
    }

    stream.getTracks().forEach(
        track => track.stop()
    );

    stream = null;

    camera.srcObject = null;
}


closeCamera.addEventListener("click", () => {

    stopCamera();

    showSection(homeSection);
});


retakeButton.addEventListener("click", async () => {

    selectedFile = null;
    uploadedStoragePath = null;
    extractedData = null;
    currentFileId = null;

    showSection(cameraSection);

    try {

        stream = await navigator.mediaDevices.getUserMedia({
            video: {
                facingMode: {
                    ideal: "environment"
                }
            },
            audio: false
        });

        camera.srcObject = stream;

    } catch (error) {

        console.error("Camera error:", error);

        alert(
            "Unable to access camera."
        );

        showSection(homeSection);
    }
});


backPreview.addEventListener("click", () => {

    resetScanner();
});


backResult.addEventListener("click", () => {

    resetScanner();
});


processButton.addEventListener("click", async () => {

    if (!selectedFile) {

        alert(
            "Please select or capture an image."
        );

        return;
    }

    processButton.disabled = true;
    processButton.textContent = "Processing...";

    showSection(resultSection);

    loading.classList.remove("hidden");
    resultContent.classList.add("hidden");

    try {
        console.log(
            "Step 1: Requesting Supabase upload URL..."
        );

        const uploadUrlResponse = await fetch(
            "/api/upload-url",
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    filename: selectedFile.name,
                    mime_type: selectedFile.type
                })
            }
        );

        const uploadData =
            await uploadUrlResponse.json();

        if (!uploadUrlResponse.ok || !uploadData.success) {

            throw new Error(
                uploadData.error ||
                "Failed to prepare file upload."
            );
        }

        console.log(
            "Upload URL received."
        );

        console.log(
            "Storage path:",
            uploadData.storage_path
        );

        console.log(
            "Step 2: Uploading image to Supabase Storage..."
        );

        const storageResponse = await fetch(
            uploadData.upload_url,
            {
                method: "PUT",

                headers: {
                    "Content-Type": selectedFile.type
                },

                body: selectedFile
            }
        );

        if (!storageResponse.ok) {

            const storageError =
                await storageResponse.text();

            console.error(
                "Supabase upload error:",
                storageError
            );

            throw new Error(
                "Failed to upload image to Supabase Storage."
            );
        }

        uploadedStoragePath =
            uploadData.storage_path;

        console.log(
            "Image successfully uploaded to Supabase Storage."
        );


        console.log(
            "Step 3: Sending storage path to Flask..."
        );

        const processResponse = await fetch(
            "/api/process-upload",
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({

                    storage_path:
                        uploadData.storage_path,

                    original_filename:
                        selectedFile.name,

                    mime_type:
                        selectedFile.type

                })
            }
        );

        const processData =
            await processResponse.json();

        if (
            !processResponse.ok ||
            !processData.success
        ) {

            throw new Error(
                processData.error ||
                "Failed to process image."
            );
        }


        console.log(
            "AI processing completed."
        );

        extractedData =
            processData.data;

        currentFileId =
            processData.data.file_id;

        console.log(
            "File ID:",
            currentFileId
        );

        console.log(
            "Extracted transactions:",
            extractedData.transactions
        );


        renderTable(
            extractedData.transactions
        );

        loading.classList.add("hidden");

        resultContent.classList.remove("hidden");

    } catch (error) {

        console.error(
            "Scanner error:",
            error
        );

        loading.classList.add("hidden");

        alert(
            "Failed to process image:\n" +
            error.message
        );

        showSection(previewSection);

    } finally {

        processButton.disabled = false;

        processButton.textContent =
            "Process Image";
    }
});


function renderTable(transactions) {

    resultTable.innerHTML = "";

    if (
        !transactions ||
        transactions.length === 0
    ) {

        const row =
            document.createElement("tr");

        row.innerHTML = `
            <td colspan="2">
                No transactions were detected.
            </td>
        `;

        resultTable.appendChild(row);

        return;
    }


    transactions.forEach(
        transaction => {

            const row =
                document.createElement("tr");

            row.innerHTML = `
                <td>
                    ${escapeHtml(
                        transaction.description
                    )}
                </td>

                <td>
                    ${escapeHtml(
                        transaction.category || "Other"
                    )}
                </td>
            `;

            resultTable.appendChild(row);
        }
    );
}


function escapeHtml(value) {

    return String(
        value ?? ""
    )
    .replaceAll(
        "&",
        "&amp;"
    )
    .replaceAll(
        "<",
        "&lt;"
    )
    .replaceAll(
        ">",
        "&gt;"
    )
    .replaceAll(
        '"',
        "&quot;"
    )
    .replaceAll(
        "'",
        "&#039;"
    );
}


csvButton.addEventListener(
    "click",
    async () => {

        if (!currentFileId) {

            alert(
                "No processed file available."
            );

            return;
        }

        window.location.href =
            `/api/download/csv/${currentFileId}`;
    }
);
document
    .getElementById("logoutButton")
    .addEventListener(
        "click",
        async function() {

            await fetch(
                "/api/logout",
                {
                    method: "POST"
                }
            );

            window.location.href =
                "/login";

        }
    );
