async function loadUser() {

    try {

        const response = await fetch("/api/me");
        const data = await response.json();

        // Jika belum login
        if (!data.authenticated) {

            window.location.href = "/login";

            return;
        }

        document.getElementById("userInfo").textContent =
            "Logged in as: " + data.email;


        let username = data.email;

        if (data.email && data.email.includes("@")) {

            username =
                data.email.split("@")[0];
        }

        document.getElementById("username").textContent =
            username;


    } catch (error) {

        console.error(
            "Failed to load user:",
            error
        );

    }
}


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


async function loadHistory() {

    const container =
        document.getElementById(
            "historyContainer"
        );

    try {

        const response = await fetch("/api/files");

        if (!response.ok) {

            throw new Error(
                "Failed to load history"
            );
        }

        const data = await response.json();

        const files =
            data.files ||
            data.data ||
            [];


        if (files.length === 0) {

            container.innerHTML = `
                <div class="empty-history">
                    No recent activity yet
                </div>
            `;

            return;
        }


        // Bersihkan container
        container.innerHTML = "";

        const recentFiles =
            files.slice(0, 5);


        recentFiles.forEach(function(file) {

            const fileName =
                file.name ||
                "Unnamed file";

            const fileType =
                file.file_type ||
                "file";

            const source =
                file.source ||
                "uploaded";


            let dateText = "";


            if (file.created_at) {

                const date =
                    new Date(
                        file.created_at
                    );

                dateText =
                    date.toLocaleDateString(
                        "en-US",
                        {
                            year: "numeric",
                            month: "short",
                            day: "numeric"
                        }
                    );
            }

            const item =
                document.createElement(
                    "div"
                );

            item.className =
                "history-item";


            const icon =
                fileType === "csv"
                    ? "📊"
                    : "📄";


            item.innerHTML = `

                <div class="history-left">

                    <div class="file-icon">
                        ${icon}
                    </div>

                    <div>

                        <div class="file-name">
                            ${fileName}
                        </div>

                        <div class="file-info">
                            ${source}
                            ${
                                dateText
                                    ? " • " + dateText
                                    : ""
                            }
                        </div>

                    </div>

                </div>

                <div class="file-type">
                    ${fileType.toUpperCase()}
                </div>

            `;


            container.appendChild(item);

        });


    } catch (error) {

        console.error(
            "History error:",
            error
        );

        container.innerHTML = `
            <div class="empty-history">
                No recent activity yet
            </div>
        `;

    }
}

loadUser();

loadHistory();