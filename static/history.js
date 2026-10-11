
document.addEventListener("DOMContentLoaded", () => {
    const searchInput = document.getElementById("historySearch");
    const typeFilter = document.getElementById("typeFilter");
    const sortOrder = document.getElementById("sortOrder");
    const historyList = document.getElementById("historyList");
    const visibleCount = document.getElementById("visibleCount");
    const emptyState = document.getElementById("emptyState");
    const emptyTitle = document.getElementById("emptyTitle");
    const emptyDescription = document.getElementById("emptyDescription");

    if (!historyList) return;

    const cards = Array.from(historyList.querySelectorAll(".history-card"));

    function formatDates() {
        document.querySelectorAll(".formatted-date").forEach((element) => {
            const rawDate = element.getAttribute("datetime");
            if (!rawDate) {
                element.textContent = "Tanggal tidak tersedia";
                return;
            }

            const date = new Date(rawDate);
            if (Number.isNaN(date.getTime())) return;

            element.textContent = new Intl.DateTimeFormat("id-ID", {
                day: "2-digit",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit"
            }).format(date);
        });
    }

    function createElement(tag, className, text) {
        const element = document.createElement(tag);
        if (className) element.className = className;
        if (text !== undefined) element.textContent = text;
        return element;
    }

    async function loadDetails(button, details) {
        details.hidden = false;
        button.setAttribute("aria-expanded", "true");
        button.innerHTML = 'Tutup <span aria-hidden="true">&#9652;</span>';
        details.replaceChildren(
            createElement("div", "detail-loading", "Memuat detail file...")
        );

        try {
            const response = await fetch(
                `/api/history/${encodeURIComponent(button.dataset.fileId)}/details`
            );

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || "Gagal memuat detail file.");
            }

            const content = createElement("div", "detail-content");

            const originalSection = createElement("section", "detail-section");
            originalSection.appendChild(
                createElement("h3", "section-title", "Dokumen asli")
            );

            if (data.original_url) {
                const imageLink = createElement("a", "image-preview-link");
                imageLink.href = data.original_url;
                imageLink.target = "_blank";
                imageLink.rel = "noopener noreferrer";
                imageLink.setAttribute("aria-label", "Buka gambar asli");

                const image = createElement("img", "original-preview");
                image.src = data.original_url;
                image.alt = `Gambar asli ${data.name || ""}`;
                image.loading = "lazy";

                image.onerror = () => {
                    imageLink.replaceChildren(
                        createElement("span", "preview-empty", "Gambar tidak dapat ditampilkan.")
                    );
                };

                imageLink.appendChild(image);
                originalSection.appendChild(imageLink);
            } else {
                originalSection.appendChild(
                    createElement("p", "preview-empty", "Dokumen asli tidak tersedia.")
                );
            }

            content.appendChild(originalSection);

            const csvSection = createElement("section", "download-section");
            csvSection.appendChild(
                createElement("h3", "section-title", "Hasil pengolahan")
            );

            if (data.csv_available) {
                const downloadCard = createElement("div", "download-card");
                const fileIcon = createElement("div", "csv-icon", "CSV");
                const fileInfo = createElement("div", "download-info");
                fileInfo.appendChild(
                    createElement("strong", "download-name", data.csv_name || "Hasil olahan.csv")
                );
                fileInfo.appendChild(
                    createElement("span", "download-caption", "File hasil pengolahan")
                );

                const downloadLink = createElement("a", "download-button", "Unduh CSV");
                downloadLink.href =
                    `/history/${encodeURIComponent(button.dataset.fileId)}/download`;

                downloadCard.append(fileIcon, fileInfo, downloadLink);
                csvSection.appendChild(downloadCard);
            } else {
                csvSection.appendChild(
                    createElement("p", "preview-empty", "File CSV hasil pengolahan belum tersedia.")
                );
            }

            content.appendChild(csvSection);
            details.replaceChildren(content);
            details.dataset.loaded = "true";
        } catch (error) {
            const errorBox = createElement("div", "detail-error");
            errorBox.appendChild(
                createElement("p", "", error.message || "Terjadi kesalahan.")
            );

            const retryButton = createElement("button", "retry-button", "Coba lagi");
            retryButton.type = "button";
            retryButton.addEventListener("click", () => {
                loadDetails(button, details);
            });

            errorBox.appendChild(retryButton);
            details.replaceChildren(errorBox);
        }
    }

    historyList.querySelectorAll(".details-toggle").forEach((button) => {
        button.addEventListener("click", async () => {
            const card = button.closest(".history-card");
            const details = card.querySelector(".card-details");
            const expanded = button.getAttribute("aria-expanded") === "true";

            if (expanded) {
                details.hidden = true;
                button.setAttribute("aria-expanded", "false");
                button.innerHTML = 'Detail <span aria-hidden="true">&#9662;</span>';
                return;
            }

            if (details.dataset.loaded === "true") {
                details.hidden = false;
                button.setAttribute("aria-expanded", "true");
                button.innerHTML = 'Tutup <span aria-hidden="true">&#9652;</span>';
                return;
            }

            await loadDetails(button, details);
        });
    });

    function applyFilters() {
        const searchTerm = searchInput.value.trim().toLowerCase();
        const selectedType = typeFilter.value;
        const selectedSort = sortOrder.value;

        const filteredCards = cards.filter((card) => {
            const name = card.dataset.name || "";
            const source = card.dataset.source || "";
            const type = card.dataset.type || "";

            const matchesSearch =
                name.includes(searchTerm) ||
                source.includes(searchTerm) ||
                type.includes(searchTerm);

            let matchesType = true;

            if (selectedType === "receipt" || selectedType === "csv") {
                matchesType = type === selectedType;
            } else if (selectedType === "other") {
                matchesType = type !== "receipt" && type !== "csv";
            }

            return matchesSearch && matchesType;
        });

        filteredCards.sort((a, b) => {
            if (selectedSort === "name") {
                return (a.dataset.name || "").localeCompare(
                    b.dataset.name || "", "id"
                );
            }

            const dateA = Date.parse(a.dataset.date || "") || 0;
            const dateB = Date.parse(b.dataset.date || "") || 0;

            return selectedSort === "oldest"
                ? dateA - dateB
                : dateB - dateA;
        });

        cards.forEach((card) => {
            card.hidden = true;
        });

        filteredCards.forEach((card) => {
            card.hidden = false;
            historyList.insertBefore(card, emptyState);
        });

        visibleCount.textContent = filteredCards.length;
        emptyState.hidden = filteredCards.length > 0;

        if (filteredCards.length === 0) {
            if (cards.length === 0) {
                emptyTitle.textContent = "Belum ada riwayat file";
                emptyDescription.textContent =
                    "File yang kamu proses akan muncul di sini.";
            } else {
                emptyTitle.textContent = "File tidak ditemukan";
                emptyDescription.textContent =
                    "Coba kata kunci atau filter yang berbeda.";
            }
        }
    }

    searchInput.addEventListener("input", applyFilters);
    typeFilter.addEventListener("change", applyFilters);
    sortOrder.addEventListener("change", applyFilters);

    formatDates();
    applyFilters();
});