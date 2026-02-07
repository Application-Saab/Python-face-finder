let eventSource = null;
let searchMatches = [];
let searchReader = null;

function switchTab(tabName, event) {
    document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
    document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));

    document.getElementById(tabName).classList.add('active');
    event.target.classList.add('active');

    if (tabName === 'albums') {
        loadAlbums();
    }
}

function getServerUrl() {
    return "";
}

function showStatus(elementId, message, type = "info") {
    const element = document.getElementById(elementId);
    element.innerHTML = message;
    element.className = `status-message show ${type}`;
}

function updateFileName() {
    const files = document.getElementById("imageFile").files;
    const fileNameDiv = document.getElementById("fileName");

    if (files.length > 0) {
        fileNameDiv.textContent = `✓ ${files.length} file(s) selected`;
        fileNameDiv.style.display = "block";
    } else {
        fileNameDiv.style.display = "none";
    }
}

function updateSampleFileName() {
    const file = document.getElementById("sampleImageFile").files[0];
    const fileNameDiv = document.getElementById("sampleFileName");
    const previewDiv = document.getElementById("samplePreview");
    const previewImg = document.getElementById("samplePreviewImg");

    if (file) {
        fileNameDiv.textContent = `✓ ${file.name}`;
        fileNameDiv.style.display = "block";

        const reader = new FileReader();
        reader.onload = e => {
            previewImg.src = e.target.result;
            previewDiv.style.display = "block";
        };
        reader.readAsDataURL(file);
    } else {
        fileNameDiv.style.display = "none";
        previewDiv.style.display = "none";
    }
}

async function uploadImage() {
    const albumName = document.getElementById("albumNameUpload").value.trim();
    const files = document.getElementById("imageFile").files;

    if (!albumName) {
        showStatus("uploadStatus", "❌ Please enter album name", "error");
        return;
    }

    if (!files.length) {
        showStatus("uploadStatus", "❌ Please select image(s)", "error");
        return;
    }

    showStatus(
        "uploadStatus",
        `<span class="spinner"></span>Uploading ${files.length} image(s)...`,
        "loading"
    );

    const formData = new FormData();
    formData.append("folder_name", albumName);
    for (const file of files) {
        formData.append("images", file);
    }

    try {
        const res = await fetch(`/upload`, { method: "POST", body: formData });
        const data = await res.json();

        if (res.ok) {
            showStatus(
                "uploadStatus",
                `✅ ${data.results.filter(r => r.status === "success").length} / ${data.total_files} images uploaded`,
                "success"
            );
            loadAlbums();
        } else {
            showStatus("uploadStatus", `❌ Error: ${data.detail}`, "error");
        }
    } catch (err) {
        showStatus("uploadStatus", `❌ Upload failed: ${err.message}`, "error");
    }
}

async function startSearch() {
    const sampleImage = document.getElementById("sampleImageFile").files[0];
    const album = document.getElementById("searchAlbum").value.trim();

    if (!sampleImage || !album) {
        showStatus("searchStatus", "❌ Sample image & album required", "error");
        return;
    }

    const formData = new FormData();
    formData.append("sample_image", sampleImage);
    formData.append("folder_name", album);
    formData.append("customer_id", "696b6f40209464d5ab917110");

    showStatus("searchStatus", `<span class="spinner"></span>Searching...`, "loading");

    const response = await fetch("/search", { method: "POST", body: formData });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();

    let buffer = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();

        for (const line of lines) {
            if (line.startsWith("data: ")) {
                handleSearchMessage(line.slice(6), album);
            }
        }
    }
}

function handleSearchMessage(message, album) {
    if (message.startsWith("✅ MATCH")) {
        const regex = /MATCH (\d+).*?\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*([\d.]+)%/;
        const m = message.match(regex);
        if (!m) return;

        const [, index, , path, confidence] = m;
        const imageUrl = `https://photography-hora.s3.eu-north-1.amazonaws.com/${path}`;

        document.getElementById("resultsGallery").innerHTML += `
            <div class="image-card">
                <img src="${imageUrl}">
                <div class="confidence">${confidence}%</div>
            </div>
        `;
    }

    if (message.includes("Search Complete")) {
        showStatus("searchStatus", "✅ Search complete", "success");
    }
}

async function loadAlbums() {
    const res = await fetch("/list");
    const data = await res.json();
    console.log(data);
}

function closeModal() {
    document.getElementById("imageModal").style.display = "none";
}
