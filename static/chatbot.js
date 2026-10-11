
const historyList = document.getElementById("historyList");
const messagesArea = document.getElementById("messagesArea");
const messageList = document.getElementById("messageList");
const welcomeScreen = document.getElementById("welcomeScreen");
const chatTitle = document.getElementById("chatTitle");
const chatForm = document.getElementById("chatForm");
const messageInput = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const sidebar = document.getElementById("sidebar");
const mobileOverlay = document.getElementById("mobileOverlay");

let activeConversationId = null;
let sending = false;

async function api(url, options = {}) {
    const response = await fetch(url, {
        ...options,
        headers: {
            "Content-Type": "application/json",
            ...(options.headers || {})
        }
    });

    const data = await response.json().catch(() => ({}));

    if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Please log in again.");
    }

    if (!response.ok || data.success === false) {
        throw new Error(data.error || "Something went wrong.");
    }

    return data;
}

function closeSidebar() {
    sidebar.classList.remove("open");
    mobileOverlay.classList.remove("visible");
}

function openSidebar() {
    sidebar.classList.add("open");
    mobileOverlay.classList.add("visible");
}

document.getElementById("openSidebar").addEventListener("click", openSidebar);
document.getElementById("closeSidebar").addEventListener("click", closeSidebar);
mobileOverlay.addEventListener("click", closeSidebar);

function showNotice(message) {
    alert(message);
}

function renderHistory(conversations) {
    historyList.replaceChildren();

    if (!conversations.length) {
        const empty = document.createElement("p");
        empty.className = "history-empty";
        empty.textContent = "Your conversations will appear here.";
        historyList.appendChild(empty);
        return;
    }

    conversations.forEach(conversation => {
        const item = document.createElement("div");
        item.className = "history-item";

        if (conversation.id === activeConversationId) {
            item.classList.add("selected");
        }

        const openButton = document.createElement("button");
        openButton.className = "history-open";
        openButton.textContent = conversation.title || "New chat";
        openButton.title = conversation.title || "New chat";
        openButton.addEventListener("click", () => {
            openConversation(conversation.id);
        });

        const menuButton = document.createElement("button");
        menuButton.className = "history-menu";
        menuButton.textContent = "⋯";
        menuButton.title = "Conversation options";
        menuButton.setAttribute("aria-label", "Conversation options");

        menuButton.addEventListener("click", async () => {
            const action = prompt(
                "Type rename to rename this chat, or delete to delete it."
            );

            if (!action) return;

            if (action.toLowerCase() === "rename") {
                const title = prompt("Enter a new title:", conversation.title || "");

                if (!title || !title.trim()) return;

                try {
                    await api(`/api/conversations/${conversation.id}`, {
                        method: "PATCH",
                        body: JSON.stringify({ title: title.trim() })
                    });

                    await loadConversations();
                    if (activeConversationId === conversation.id) {
                        chatTitle.textContent = title.trim();
                    }
                } catch (error) {
                    showNotice(error.message);
                }
            } else if (action.toLowerCase() === "delete") {
                if (!confirm(`Delete "${conversation.title || "New chat"}"? This cannot be undone.`)) {
                    return;
                }

                try {
                    await api(`/api/conversations/${conversation.id}`, {
                        method: "DELETE"
                    });

                    if (activeConversationId === conversation.id) {
                        activeConversationId = null;
                        resetChatScreen();
                    }

                    await loadConversations();
                } catch (error) {
                    showNotice(error.message);
                }
            }
        });

        item.append(openButton, menuButton);
        historyList.appendChild(item);
    });
}

async function loadConversations() {
    try {
        const data = await api("/api/conversations");
        renderHistory(data.conversations || []);
    } catch (error) {
        historyList.replaceChildren();
        const empty = document.createElement("p");
        empty.className = "history-empty";
        empty.textContent = error.message;
        historyList.appendChild(empty);
    }
}

function resetChatScreen() {
    messageList.replaceChildren();
    welcomeScreen.hidden = false;
    chatTitle.textContent = "New chat";
    messageInput.value = "";
    activeConversationId = null;
    renderHistoryFromCurrentState();
}

async function renderHistoryFromCurrentState() {
    await loadConversations();
}

function appendMessage(role, content) {
    const message = document.createElement("div");
    message.className = `message ${role === "user" ? "user" : "assistant"}`;

    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.textContent = role === "user" ? "Y" : "R";

    const body = document.createElement("div");
    body.className = "message-content";

    // Use textContent rather than innerHTML to prevent injected HTML/scripts.
    // Line breaks are preserved by CSS white-space: pre-wrap.
    body.textContent = content || "";

    message.append(avatar, body);
    messageList.appendChild(message);
    welcomeScreen.hidden = true;

    messagesArea.scrollTop = messagesArea.scrollHeight;
}

async function openConversation(conversationId) {
    try {
        const data = await api(`/api/conversations/${conversationId}`);

        activeConversationId = conversationId;
        chatTitle.textContent = data.conversation.title || "New chat";
        messageList.replaceChildren();

        const messages = data.messages || [];

        if (!messages.length) {
            welcomeScreen.hidden = false;
        } else {
            welcomeScreen.hidden = true;

            messages.forEach(message => {
                appendMessage(message.role, message.content);
            });
        }

        await loadConversations();
        closeSidebar();
    } catch (error) {
        showNotice(error.message);
    }
}

async function createNewChat() {
    if (sending) return;

    try {
        const data = await api("/api/conversations", {
            method: "POST",
            body: JSON.stringify({ title: "New chat" })
        });

        activeConversationId = data.conversation.id;
        messageList.replaceChildren();
        welcomeScreen.hidden = false;
        chatTitle.textContent = "New chat";
        messageInput.value = "";

        await loadConversations();
        closeSidebar();
        messageInput.focus();
    } catch (error) {
        showNotice(error.message);
    }
}

document.getElementById("newChatButton").addEventListener("click", createNewChat);
document.getElementById("refreshHistory").addEventListener("click", loadConversations);

chatForm.addEventListener("submit", async event => {
    event.preventDefault();

    const content = messageInput.value.trim();

    if (!content || sending) return;

    sending = true;
    sendButton.disabled = true;
    messageInput.disabled = true;

    try {
        // Create a persistent conversation on the first message.
        if (!activeConversationId) {
            const created = await api("/api/conversations", {
                method: "POST",
                body: JSON.stringify({ title: "New chat" })
            });

            activeConversationId = created.conversation.id;
        }

        appendMessage("user", content);
        messageInput.value = "";
        messageInput.style.height = "auto";

        const loading = document.createElement("div");
        loading.className = "message assistant";
        loading.id = "assistantLoading";

        const loadingAvatar = document.createElement("div");
        loadingAvatar.className = "message-avatar";
        loadingAvatar.textContent = "R";

        const loadingText = document.createElement("div");
        loadingText.className = "message-content";
        loadingText.textContent = "Thinking...";

        loading.append(loadingAvatar, loadingText);
        messageList.appendChild(loading);
        messagesArea.scrollTop = messagesArea.scrollHeight;

        let data;

        try {
            data = await api(
                `/api/conversations/${activeConversationId}/messages`,
                {
                    method: "POST",
                    body: JSON.stringify({ content })
                }
            );
        } finally {
            loading.remove();
        }

        appendMessage("assistant", data.assistant_message.content);

        chatTitle.textContent = data.title || chatTitle.textContent;
        await loadConversations();
    } catch (error) {
        showNotice(error.message);
        await loadConversations();
    } finally {
        sending = false;
        sendButton.disabled = false;
        messageInput.disabled = false;
        messageInput.focus();
    }
});

messageInput.addEventListener("input", () => {
    messageInput.style.height = "auto";
    messageInput.style.height = `${Math.min(messageInput.scrollHeight, 180)}px`;
});

messageInput.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatForm.requestSubmit();
    }
});

document.querySelectorAll(".suggestion").forEach(button => {
    button.addEventListener("click", () => {
        messageInput.value = button.dataset.prompt || "";
        messageInput.focus();
        messageInput.style.height = "auto";
        messageInput.style.height = `${Math.min(messageInput.scrollHeight, 180)}px`;
    });
});

document.getElementById("renameButton").addEventListener("click", async () => {
    if (!activeConversationId) {
        showNotice("Send a message first to create a conversation.");
        return;
    }

    const title = prompt("Enter a new conversation title:", chatTitle.textContent);

    if (!title || !title.trim()) return;

    try {
        await api(`/api/conversations/${activeConversationId}`, {
            method: "PATCH",
            body: JSON.stringify({ title: title.trim() })
        });

        chatTitle.textContent = title.trim();
        await loadConversations();
    } catch (error) {
        showNotice(error.message);
    }
});

document.getElementById("logoutButton").addEventListener("click", async () => {
    if (!confirm("Are you sure you want to log out?")) return;

    try {
        await fetch("/api/logout", { method: "POST" });
    } finally {
        window.location.href = "/login";
    }
});

resetChatScreen();
loadConversations();