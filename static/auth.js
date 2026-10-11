const loginForm = document.getElementById("loginForm");
const registerForm = document.getElementById("registerForm");

if (loginForm) {
    loginForm.addEventListener("submit", async function (e) {
        e.preventDefault();

        const email = document.getElementById("email").value;
        const password = document.getElementById("password").value;
        const button = document.getElementById("loginButton");
        const message = document.getElementById("message");

        button.disabled = true;
        button.textContent = "Logging in...";

        try {
            const response = await fetch("/api/login", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    email: email,
                    password: password
                })
            });

            const result = await response.json();

            if (result.success) {
                window.location.href = "/";
            } else {
                message.textContent = result.error;
            }

        } catch (error) {
            message.textContent = "Something went wrong.";
        }

        button.disabled = false;
        button.textContent = "Login";
    });
}


if (registerForm) {
    registerForm.addEventListener("submit", async function (e) {
        e.preventDefault();

        const fullName = document.getElementById("fullName").value;
        const email = document.getElementById("email").value;
        const password = document.getElementById("password").value;
        const confirmPassword = document.getElementById("confirmPassword").value;

        const button = document.getElementById("registerButton");
        const message = document.getElementById("message");

        if (password !== confirmPassword) {
            message.textContent = "Passwords do not match.";
            return;
        }

        button.disabled = true;
        button.textContent = "Creating account...";

        try {
            const response = await fetch("/api/register", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    full_name: fullName,
                    email: email,
                    password: password
                })
            });

            const result = await response.json();

            if (result.success) {
                message.textContent =
                    "Registration successful. You can now login.";

                setTimeout(() => {
                    window.location.href = "/login";
                }, 1500);

            } else {
                message.textContent = result.error;
            }

        } catch (error) {
            message.textContent = "Something went wrong.";
        }

        button.disabled = false;
        button.textContent = "Register";
    });
}