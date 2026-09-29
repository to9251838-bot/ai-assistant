const state = {
  token: localStorage.getItem("ai_orb_token"),
  email: localStorage.getItem("ai_orb_email"),
  recording: false,
  mediaRecorder: null,
  stream: null,
  audioContext: null,
  analyser: null,
  raf: null,
  silenceStarted: null,
  speechStarted: null,
  authMode: "login"
};

const orb = document.getElementById("orb");
const statusEl = document.getElementById("status");
const hintEl = document.getElementById("hint");
const messagesEl = document.getElementById("messages");
const composer = document.getElementById("composer");
const textInput = document.getElementById("textInput");
const language = document.getElementById("language");
const authButton = document.getElementById("authButton");
const connection = document.getElementById("connection");

const authModal = document.getElementById("authModal");
const closeModal = document.getElementById("closeModal");
const authForm = document.getElementById("authForm");
const authTitle = document.getElementById("authTitle");
const authSubtitle = document.getElementById("authSubtitle");
const authSubmit = document.getElementById("authSubmit");
const toggleAuth = document.getElementById("toggleAuth");
const authError = document.getElementById("authError");
const emailInput = document.getElementById("email");
const passwordInput = document.getElementById("password");


function setStatus(text, hint = "") {
  statusEl.textContent = text;
  hintEl.textContent = hint;
}


function apiHeaders(json = true) {
  const headers = {};

  if (json) {
    headers["Content-Type"] = "application/json";
  }

  if (state.token) {
    headers["Authorization"] = `Bearer ${state.token}`;
  }

  return headers;
}


function clearEmpty() {
  const empty = messagesEl.querySelector(".empty-message");

  if (empty) {
    empty.remove();
  }
}


function addMessage(role, text, image = null) {
  clearEmpty();

  const div = document.createElement("div");
  div.className = `message ${role}`;

  if (text) {
    const textNode = document.createElement("div");
    textNode.textContent = text;
    div.appendChild(textNode);
  }

  if (image) {
    const img = document.createElement("img");

    img.src = image;
    img.alt = "Generated image";

    div.appendChild(img);
  }

  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}


function speak(text, audioUrl) {
  /*
    Если сервер прислал аудио от Groq —
    проигрываем его.
  */
  if (audioUrl) {
    const audio = new Audio(audioUrl);

    audio.play().catch(() => {});

    return;
  }

  /*
    Для русского используем встроенный SpeechSynthesis
    браузера, потому что Groq TTS сейчас ориентирован
    на поддерживаемые им языки.
  */
  if ("speechSynthesis" in window && text) {
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);

    utterance.lang =
      language.value === "en"
        ? "en-US"
        : "ru-RU";

    utterance.rate = 0.98;

    window.speechSynthesis.speak(utterance);
  }
}


async function loadConversation() {
  if (!state.token) {
    return;
  }

  try {
    const response = await fetch("/api/conversation", {
      headers: apiHeaders(false)
    });

    if (!response.ok) {
      return;
    }

    const data = await response.json();

    if (data.messages?.length) {
      messagesEl.innerHTML = "";

      for (const msg of data.messages) {
        addMessage(
          msg.role,
          msg.content || "",
          msg.image || null
        );
      }
    }
  } catch {
    /*
      Если история недоступна,
      интерфейс всё равно продолжает работать.
    */
  }
}


async function sendText(text) {
  if (!state.token) {
    openAuth();
    return;
  }

  addMessage("user", text);

  setStatus(
    "Думаю…",
    "Подожди немного"
  );

  try {
    const response = await fetch("/api/text", {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({
        text,
        language: language.value
      })
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(
        data.error || "Ошибка запроса"
      );
    }

    addMessage(
      "assistant",
      data.text,
      data.image
    );

    speak(
      data.text,
      data.audio
    );

    setStatus(
      "Готов к разговору",
      "Нажми на сферу и говори"
    );

  } catch (error) {
    addMessage(
      "assistant",
      `Ошибка: ${error.message}`
    );

    setStatus(
      "Произошла ошибка",
      "Попробуй ещё раз"
    );
  }
}


async function sendVoice(blob) {
  if (!state.token) {
    openAuth();
    return;
  }

  setStatus(
    "Распознаю речь…",
    "Обрабатываю запрос"
  );

  const form = new FormData();

  form.append(
    "audio",
    blob,
    "recording.webm"
  );

  form.append(
    "language",
    language.value
  );

  try {
    const response = await fetch(
      "/api/voice",
      {
        method: "POST",
        headers: apiHeaders(false),
        body: form
      }
    );

    const data = await response.json();

    if (!response.ok) {
      throw new Error(
        data.error || "Ошибка обработки"
      );
    }

    /*
      Показываем распознанную пользователем речь.
    */
    if (data.transcript) {
      addMessage(
        "user",
        data.transcript
      );
    }

    /*
      Ответ ассистента.
    */
    addMessage(
      "assistant",
      data.text,
      data.image
    );

    speak(
      data.text,
      data.audio
    );

    setStatus(
      "Готов к разговору",
      "Нажми на сферу и говори"
    );

  } catch (error) {
    addMessage(
      "assistant",
      `Ошибка: ${error.message}`
    );

    setStatus(
      "Произошла ошибка",
      "Проверь API-ключи в Render"
    );
  }
}


/*
  Анализ громкости микрофона.

  Чем громче голос —
  тем сильнее увеличивается Orb.
*/
function updateOrbFromVolume() {
  if (!state.analyser) {
    return;
  }

  const data =
    new Uint8Array(
      state.analyser.fftSize
    );

  state.analyser.getByteTimeDomainData(data);

  let sum = 0;

  for (const value of data) {
    const normalized =
      (value - 128) / 128;

    sum += normalized * normalized;
  }

  const rms =
    Math.sqrt(sum / data.length);

  /*
    Реакция сферы на громкость.
  */
  const scale =
    1 + Math.min(rms * 2.8, 0.23);

  orb.style.setProperty(
    "--orb-scale",
    scale.toFixed(3)
  );


  /*
    Silence detector.

    Если человек начал говорить,
    а затем молчит примерно 1.35 сек —
    запись автоматически заканчивается.
  */
  const threshold = 0.055;
  const silenceMs = 1350;
  const minSpeechMs = 1200;

  if (rms > threshold) {

    if (!state.speechStarted) {
      state.speechStarted = Date.now();
    }

    state.silenceStarted = null;

    setStatus(
      "Слушаю…",
      "Говори, я реагирую на громкость"
    );

  } else if (state.speechStarted) {

    if (!state.silenceStarted) {
      state.silenceStarted = Date.now();
    }

    if (
      Date.now() - state.silenceStarted > silenceMs &&
      Date.now() - state.speechStarted > minSpeechMs
    ) {
      stopRecording(true);
      return;
    }
  }

  state.raf =
    requestAnimationFrame(
      updateOrbFromVolume
    );
}


/*
  Начало записи.
*/
async function startRecording() {

  if (!state.token) {
    openAuth();
    return;
  }

  try {

    const stream =
      await navigator.mediaDevices.getUserMedia({
        audio: true
      });

    state.stream = stream;
    state.recording = true;

    state.silenceStarted = null;
    state.speechStarted = null;


    /*
      Используем WebM + Opus,
      если браузер поддерживает этот формат.
    */
    const preferred =
      "audio/webm;codecs=opus";

    const mimeType =
      MediaRecorder.isTypeSupported(preferred)
        ? preferred
        : "audio/webm";


    state.mediaRecorder =
      new MediaRecorder(
        stream,
        { mimeType }
      );


    const chunks = [];


    state.mediaRecorder.ondataavailable =
      event => {

        if (event.data.size > 0) {
          chunks.push(event.data);
        }

      };


    state.mediaRecorder.onstop =
      async () => {

        const blob =
          new Blob(
            chunks,
            { type: mimeType }
          );

        if (blob.size > 0) {
          await sendVoice(blob);
        }

      };


    /*
      AudioContext нужен для анализа громкости.
    */
    state.audioContext =
      new AudioContext();

    const source =
      state.audioContext
        .createMediaStreamSource(stream);

    state.analyser =
      state.audioContext.createAnalyser();

    state.analyser.fftSize = 1024;

    source.connect(
      state.analyser
    );


    state.mediaRecorder.start();

    orb.classList.add("recording");

    setStatus(
      "Слушаю…",
      "Говори"
    );


    state.raf =
      requestAnimationFrame(
        updateOrbFromVolume
      );

  } catch (error) {

    setStatus(
      "Нет доступа к микрофону",
      "Разреши микрофон в браузере"
    );

  }
}


/*
  Остановка записи.
*/
function stopRecording(send = true) {

  if (!state.recording) {
    return;
  }

  state.recording = false;

  orb.classList.remove("recording");

  orb.style.setProperty(
    "--orb-scale",
    "1"
  );


  if (state.raf) {
    cancelAnimationFrame(state.raf);
  }

  state.raf = null;


  if (
    state.mediaRecorder &&
    state.mediaRecorder.state !== "inactive"
  ) {

    /*
      Если send=false,
      не отправляем аудио.
    */
    if (!send) {
      state.mediaRecorder.onstop = null;
    }

    state.mediaRecorder.stop();
  }


  if (state.stream) {

    state.stream
      .getTracks()
      .forEach(track => track.stop());

  }

  state.stream = null;


  if (state.audioContext) {

    state.audioContext
      .close()
      .catch(() => {});

  }

  state.audioContext = null;
  state.analyser = null;


  if (!send) {

    setStatus(
      "Готов к разговору",
      "Нажми на сферу и говори"
    );

  }
}


/*
  Нажатие на Orb:

  если не записываем —
  начинаем запись.

  если записываем —
  останавливаем запись.
*/
orb.addEventListener(
  "click",
  async () => {

    if (state.recording) {

      stopRecording(true);

    } else {

      await startRecording();

    }

  }
);


/*
  Отправка сообщения из текстового поля.
*/
composer.addEventListener(
  "submit",
  event => {

    event.preventDefault();

    const text =
      textInput.value.trim();

    if (!text) {
      return;
    }

    textInput.value = "";

    sendText(text);

  }
);


/*
  Открыть окно авторизации.
*/
function openAuth() {

  authModal.classList.remove(
    "hidden"
  );

  authError.textContent = "";

  emailInput.focus();

}


/*
  Закрыть окно авторизации.
*/
function closeAuth() {

  authModal.classList.add(
    "hidden"
  );

}


/*
  Обновить кнопку входа.
*/
function updateAuthUI() {

  if (state.email) {

    authButton.textContent =
      state.email;

    authButton.title =
      "Нажми, чтобы выйти";

  } else {

    authButton.textContent =
      "Войти";

    authButton.title = "";

  }

}


/*
  Login / Logout.
*/
authButton.addEventListener(
  "click",
  () => {

    if (state.token) {

      localStorage.removeItem(
        "ai_orb_token"
      );

      localStorage.removeItem(
        "ai_orb_email"
      );

      state.token = null;
      state.email = null;

      updateAuthUI();

      setStatus(
        "Готов к разговору",
        "Войди, чтобы сохранять историю"
      );

    } else {

      openAuth();

    }

  }
);


/*
  Закрытие модального окна.
*/
closeModal.addEventListener(
  "click",
  closeAuth
);


document
  .querySelector(".modal-backdrop")
  .addEventListener(
    "click",
    closeAuth
  );


/*
  Переключение:

  Вход <-> Регистрация
*/
toggleAuth.addEventListener(
  "click",
  () => {

    state.authMode =
      state.authMode === "login"
        ? "register"
        : "login";

    const register =
      state.authMode === "register";


    authTitle.textContent =
      register
        ? "Регистрация"
        : "Вход";


    authSubtitle.textContent =
      register
        ? "Создай аккаунт для сохранения разговоров."
        : "Войди, чтобы сохранять разговоры.";


    authSubmit.textContent =
      register
        ? "Создать аккаунт"
        : "Войти";


    toggleAuth.textContent =
      register
        ? "У меня уже есть аккаунт"
        : "Создать аккаунт";


    authError.textContent = "";

  }
);


/*
  Авторизация.
*/
authForm.addEventListener(
  "submit",
  async event => {

    event.preventDefault();

    const email =
      emailInput.value.trim();

    const password =
      passwordInput.value;

    authError.textContent =
      "Подожди…";


    try {

      const endpoint =
        state.authMode === "register"
          ? "/api/auth/register"
          : "/api/auth/login";


      const response =
        await fetch(
          endpoint,
          {
            method: "POST",

            headers: {
              "Content-Type":
                "application/json"
            },

            body: JSON.stringify({
              email,
              password
            })
          }
        );


      const data =
        await response.json();


      if (!response.ok) {

        throw new Error(
          data.error ||
          "Ошибка авторизации"
        );

      }


      state.token =
        data.token;

      state.email =
        data.email;


      localStorage.setItem(
        "ai_orb_token",
        state.token
      );

      localStorage.setItem(
        "ai_orb_email",
        state.email
      );


      updateAuthUI();

      closeAuth();

      passwordInput.value = "";

      await loadConversation();


      setStatus(
        "Готов к разговору",
        "Нажми на сферу и говори"
      );


    } catch (error) {

      authError.textContent =
        error.message;

    }

  }
);


/*
  Проверяем состояние сервера.
*/
async function checkHealth() {

  try {

    const response =
      await fetch(
        "/api/health"
      );

    const data =
      await response.json();


    connection.textContent =
      data.ok ? "●" : "○";


    connection.title =
      JSON.stringify(data);

  } catch {

    connection.textContent =
      "○";

  }

}


/*
  Запуск приложения.
*/
updateAuthUI();

checkHealth();

loadConversation();
