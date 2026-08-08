let languageData = null;
    let maxChars = 0;
    let activeJobId = null;
    let lastCompletedJob = null;
    let currentInputTab = "textarea";
    let currentSourceFormat = "txt";
    let currentOriginalExtension = "txt";
    let fullResultText = "";
    let currentUiLanguage = localStorage.getItem("linguinator_ui_language") || "en";
    let historyItems = [];
    let historyVisibleCount = 5;
    const seenCompletedJobIds = new Set();
    const HISTORY_PAGE_SIZE = 5;
    const baseTitle = document.title || "Linguinator";
    const originalExportExtensions = ["docx", "odt", "pptx", "csv", "xlsx", "html", "htm", "srt", "vtt", "json", "yaml", "yml", "po", "xlf", "xliff"];
    const outputFormatKeys = {txt: "formatTxt", md: "formatMarkdown", pdf: "formatPdf", original: "formatOriginal"};
    const uiText = {
      en: {
        uiLanguage: "UI Language",
        subtitle: "Local translation workbench for text and document workflows.",
        queue: "Queue",
        source: "Source",
        target: "Target",
        searchLanguage: "Search language...",
        favorites: "Favorites",
        allLanguages: "All languages",
        loading: "Loading...",
        textField: "Text Field",
        text: "Text",
        markdown: "Markdown",
        officeDoc: "Doc File",
        pptxFile: "PowerPoint",
        csvFile: "CSV File",
        pdf: "PDF",
        textFile: "Text File",
        markdownFile: "Markdown File",
        loadFile: "Load File",
        plaintext: "Plaintext",
        translateInput: "Translate Input",
        clear: "Clear",
        pause: "Pause",
        resume: "Resume",
        stop: "Stop",
        skip: "Skip",
        delete: "Delete",
        history: "History",
        noQueuedJobs: "No queued jobs.",
        noHistory: "No saved translations yet.",
        loadMore: "Load more",
        queued: "Queued",
        queuePosition: "Queue position #{position}",
        watchJob: "Click to track this job in the progress bar and tab title.",
        started: "Started",
        chunks: "chunks",
        workerSingular: "worker",
        workerPlural: "workers",
        jobFailed: "Job failed.",
        selectFileFirst: "Select a supported text, document, table, subtitle, or localization file first.",
        selectPdfFirst: "Select a PDF first.",
        originalNeedsFile: "Original format export needs the loaded source file in the input tab.",
        extracting: "Extracting",
        starting: "Starting...",
        startingChunks: "Starting {count} chunks...",
        uploadingPdf: "Uploading PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Original Format",
        modelDedicated: "Dedicated model for this language pair.",
        modelFallback: "No dedicated model for this pair, using the multilingual fallback."
      },
      de: {
        uiLanguage: "UI-Sprache",
        subtitle: "Lokale Uebersetzungsoberflaeche fuer Text- und Dokument-Workflows.",
        queue: "Warteschlange",
        source: "Quelle",
        target: "Ziel",
        searchLanguage: "Sprache suchen...",
        favorites: "Favoriten",
        allLanguages: "Alle Sprachen",
        loading: "Laedt...",
        textField: "Textfeld",
        text: "Text",
        markdown: "Markdown",
        officeDoc: "Office-Dokument",
        pptxFile: "PowerPoint",
        csvFile: "CSV-Datei",
        pdf: "PDF",
        textFile: "Textdatei",
        markdownFile: "Markdown-Datei",
        loadFile: "Datei laden",
        plaintext: "Klartext",
        translateInput: "Eingabe uebersetzen",
        clear: "Leeren",
        pause: "Pause",
        resume: "Fortsetzen",
        stop: "Stoppen",
        skip: "Ueberspringen",
        delete: "Loeschen",
        history: "History",
        noQueuedJobs: "Keine wartenden Jobs.",
        noHistory: "Noch keine gespeicherten Uebersetzungen.",
        loadMore: "Mehr laden",
        queued: "Eingereiht",
        queuePosition: "Warteschlangenposition #{position}",
        watchJob: "Klicken, um diesen Job im Fortschrittsbalken und Tab-Titel zu verfolgen.",
        started: "Gestartet",
        chunks: "Chunks",
        workerSingular: "Worker",
        workerPlural: "Worker",
        jobFailed: "Job fehlgeschlagen.",
        selectFileFirst: "Waehle zuerst eine unterstuetzte Text-, Dokument-, Tabellen-, Untertitel- oder Lokalisierungsdatei.",
        selectPdfFirst: "Waehle zuerst eine PDF aus.",
        originalNeedsFile: "Originalformat-Export braucht die geladene Quelldatei im Eingabe-Tab.",
        extracting: "Extrahiere",
        starting: "Starte...",
        startingChunks: "Starte {count} Chunks...",
        uploadingPdf: "Lade PDF hoch...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Originalformat",
        modelDedicated: "Eigenes Modell fuer dieses Sprachpaar.",
        modelFallback: "Kein eigenes Modell fuer dieses Paar, nutzt den mehrsprachigen Fallback."
      },
      es: {
        uiLanguage: "Idioma de UI",
        subtitle: "Banco local de traduccion para flujos de texto y documentos.",
        queue: "Cola",
        source: "Origen",
        target: "Destino",
        searchLanguage: "Buscar idioma...",
        favorites: "Favoritos",
        allLanguages: "Todos los idiomas",
        loading: "Cargando...",
        textField: "Campo de texto",
        text: "Texto",
        markdown: "Markdown",
        officeDoc: "Documento Office",
        pptxFile: "PowerPoint",
        csvFile: "Archivo CSV",
        pdf: "PDF",
        textFile: "Archivo de texto",
        markdownFile: "Archivo Markdown",
        loadFile: "Cargar archivo",
        plaintext: "Texto plano",
        translateInput: "Traducir entrada",
        clear: "Limpiar",
        pause: "Pausar",
        resume: "Continuar",
        stop: "Detener",
        skip: "Omitir",
        delete: "Eliminar",
        history: "Historial",
        noQueuedJobs: "No hay trabajos en cola.",
        noHistory: "Aun no hay traducciones guardadas.",
        loadMore: "Cargar mas",
        queued: "En cola",
        queuePosition: "Posicion en cola #{position}",
        watchJob: "Haz clic para seguir este trabajo en la barra de progreso y el titulo de la pestana.",
        started: "Iniciado",
        chunks: "fragmentos",
        workerSingular: "worker",
        workerPlural: "workers",
        jobFailed: "El trabajo fallo.",
        selectFileFirst: "Selecciona primero un archivo compatible de texto, documento, tabla, subtitulos o localizacion.",
        selectPdfFirst: "Selecciona primero un PDF.",
        originalNeedsFile: "La exportacion en formato original necesita el archivo fuente cargado en la pestana de entrada.",
        extracting: "Extrayendo",
        starting: "Iniciando...",
        startingChunks: "Iniciando {count} fragmentos...",
        uploadingPdf: "Subiendo PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Formato original",
        modelDedicated: "Modelo dedicado para este par de idiomas.",
        modelFallback: "Sin modelo dedicado para este par, se usa el alternativo multilingue."
      },
      fr: {
        uiLanguage: "Langue UI",
        subtitle: "Atelier local de traduction pour les workflows texte et documents.",
        queue: "File d'attente",
        source: "Source",
        target: "Cible",
        searchLanguage: "Rechercher une langue...",
        favorites: "Favoris",
        allLanguages: "Toutes les langues",
        loading: "Chargement...",
        textField: "Champ texte",
        text: "Texte",
        markdown: "Markdown",
        officeDoc: "Document Office",
        pptxFile: "PowerPoint",
        csvFile: "Fichier CSV",
        pdf: "PDF",
        textFile: "Fichier texte",
        markdownFile: "Fichier Markdown",
        loadFile: "Charger le fichier",
        plaintext: "Texte brut",
        translateInput: "Traduire l'entree",
        clear: "Effacer",
        pause: "Pause",
        resume: "Reprendre",
        stop: "Arreter",
        skip: "Passer",
        delete: "Supprimer",
        history: "Historique",
        noQueuedJobs: "Aucun job en file.",
        noHistory: "Aucune traduction enregistree.",
        loadMore: "Charger plus",
        queued: "En file",
        queuePosition: "Position en file #{position}",
        watchJob: "Cliquer pour suivre ce job dans la barre de progression et le titre de l'onglet.",
        started: "Demarre",
        chunks: "segments",
        workerSingular: "worker",
        workerPlural: "workers",
        jobFailed: "Le job a echoue.",
        selectFileFirst: "Selectionne d'abord un fichier compatible texte, document, tableau, sous-titres ou localisation.",
        selectPdfFirst: "Selectionne d'abord un PDF.",
        originalNeedsFile: "L'export au format original a besoin du fichier source charge dans l'onglet d'entree.",
        extracting: "Extraction",
        starting: "Demarrage...",
        startingChunks: "Demarrage de {count} segments...",
        uploadingPdf: "Televersement du PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Format original",
        modelDedicated: "Modele dedie pour cette paire de langues.",
        modelFallback: "Pas de modele dedie pour cette paire, utilise le modele multilingue."
      }
    };
    const FAVORITE_LANGUAGE_COUNT = 4;
    const defaultFavoriteLanguages = ["eng_Latn", "deu_Latn", "fra_Latn", "spa_Latn"];
    const languageNames = new Intl.DisplayNames(["en"], {type: "language"});
    const languageFallbacks = {
      ace: "Acehnese",
      acm: "Mesopotamian Arabic",
      acq: "Taizzi-Adeni Arabic",
      aeb: "Tunisian Arabic",
      afr: "Afrikaans",
      amh: "Amharic",
      arb: "Modern Standard Arabic",
      ary: "Moroccan Arabic",
      arz: "Egyptian Arabic",
      asm: "Assamese",
      ast: "Asturian",
      awa: "Awadhi",
      ayr: "Aymara",
      azb: "South Azerbaijani",
      azj: "North Azerbaijani",
      bak: "Bashkir",
      bam: "Bambara",
      ban: "Balinese",
      bel: "Belarusian",
      ben: "Bengali",
      bho: "Bhojpuri",
      bod: "Tibetan",
      bos: "Bosnian",
      bul: "Bulgarian",
      cat: "Catalan",
      ceb: "Cebuano",
      ces: "Czech",
      cjk: "Chokwe",
      cym: "Welsh",
      dan: "Danish",
      deu: "German",
      ell: "Greek",
      eng: "English",
      epo: "Esperanto",
      est: "Estonian",
      eus: "Basque",
      fin: "Finnish",
      fra: "French",
      gaz: "West Central Oromo",
      gle: "Irish",
      glg: "Galician",
      guj: "Gujarati",
      hau: "Hausa",
      heb: "Hebrew",
      hin: "Hindi",
      hrv: "Croatian",
      hun: "Hungarian",
      hye: "Armenian",
      ibo: "Igbo",
      ind: "Indonesian",
      isl: "Icelandic",
      ita: "Italian",
      jav: "Javanese",
      jpn: "Japanese",
      kan: "Kannada",
      kat: "Georgian",
      kaz: "Kazakh",
      khm: "Khmer",
      kir: "Kyrgyz",
      kor: "Korean",
      lao: "Lao",
      lit: "Lithuanian",
      lvs: "Latvian",
      mal: "Malayalam",
      mar: "Marathi",
      mkd: "Macedonian",
      mlt: "Maltese",
      mya: "Burmese",
      nld: "Dutch",
      nob: "Norwegian Bokmal",
      npi: "Nepali",
      ory: "Odia",
      pan: "Punjabi",
      pes: "Western Persian",
      pol: "Polish",
      por: "Portuguese",
      ron: "Romanian",
      rus: "Russian",
      sin: "Sinhala",
      slk: "Slovak",
      slv: "Slovenian",
      spa: "Spanish",
      srp: "Serbian",
      swe: "Swedish",
      tam: "Tamil",
      tel: "Telugu",
      tha: "Thai",
      tur: "Turkish",
      ukr: "Ukrainian",
      urd: "Urdu",
      uzn: "Northern Uzbek",
      vie: "Vietnamese",
      zho: "Chinese"
    };
    const languageCountries = {
      afr: "ZA", amh: "ET", arb: "SA", asm: "IN", azj: "AZ", bel: "BY",
      ben: "BD", bos: "BA", bul: "BG", cat: "ES", ces: "CZ", cym: "GB",
      dan: "DK", deu: "DE", ell: "GR", eng: "GB", epo: "UN", est: "EE",
      eus: "ES", fin: "FI", fra: "FR", gle: "IE", glg: "ES", guj: "IN",
      hau: "NG", heb: "IL", hin: "IN", hrv: "HR", hun: "HU", hye: "AM",
      ibo: "NG", ind: "ID", isl: "IS", ita: "IT", jav: "ID", jpn: "JP",
      kan: "IN", kat: "GE", kaz: "KZ", khm: "KH", kir: "KG", kor: "KR",
      lao: "LA", lit: "LT", lvs: "LV", mal: "IN", mar: "IN", mkd: "MK",
      mlt: "MT", mya: "MM", nld: "NL", nob: "NO", npi: "NP", ory: "IN",
      pan: "IN", pes: "IR", pol: "PL", por: "PT", ron: "RO", rus: "RU",
      sin: "LK", slk: "SK", slv: "SI", spa: "ES", srp: "RS", swe: "SE",
      tam: "IN", tel: "IN", tha: "TH", tur: "TR", ukr: "UA", urd: "PK",
      uzn: "UZ", vie: "VN", zho: "CN"
    };

    function t(key, values = {}) {
      let text = (uiText[currentUiLanguage] && uiText[currentUiLanguage][key]) || uiText.en[key] || key;
      for (const [name, value] of Object.entries(values)) {
        text = text.replace("{" + name + "}", value);
      }
      return text;
    }

    function setText(selector, key) {
      const element = document.querySelector(selector);
      if (element) element.textContent = t(key);
    }

    function setTitle(selector, key) {
      const element = document.querySelector(selector);
      if (element) element.title = t(key);
    }

    function applyUiLanguage() {
      document.documentElement.lang = currentUiLanguage;
      const uiLanguage = document.getElementById("uiLanguage");
      if (uiLanguage) uiLanguage.value = currentUiLanguage;
      setText(".subtle", "subtitle");
      setText('label[for="uiLanguage"]', "uiLanguage");
      setText('label[for="queue"]', "queue");
      setText('label[for="source"]', "source");
      setText('label[for="target"]', "target");
      setText('[data-input-tab="textarea"]', "textField");
      setText('[data-input-tab="text"]', "text");
      setText('[data-input-tab="markdown"]', "markdown");
      setText('[data-input-tab="office"]', "officeDoc");
      setText('[data-input-tab="pptx"]', "pptxFile");
      setText('[data-input-tab="csv"]', "csvFile");
      setText('[data-input-tab="pdf"]', "pdf");
      setText('label[for="text"]', "textField");
      setText('label[for="pdf"]', "pdf");
      setText('label[for="pdfPlaintext"]', "plaintext");
      setText("#loadTextFile", "loadFile");
      setText("#translate", "translateInput");
      setText("#clearInput", "clear");
      setText("#pauseJob", "pause");
      setText("#resumeJob", "resume");
      setText("#stopJob", "stop");
      setText('label[for="history"]', "history");
      setTitle("#uiLanguage", "uiLanguage");
      refreshInputLabels();
      refreshInputTabSelectLabels();
      updateDownloadButtons();
      updateCounter();
      if (languageData) {
        renderSelect("source", document.getElementById("source").value);
        renderSelect("target", document.getElementById("target").value);
      }
      loadQueue().catch(() => {});
      loadHistory().catch(() => {});
    }

    function setupUiLanguagePicker() {
      const select = document.getElementById("uiLanguage");
      if (!select) return;
      select.value = currentUiLanguage;
      select.addEventListener("change", () => {
        currentUiLanguage = select.value || "en";
        localStorage.setItem("linguinator_ui_language", currentUiLanguage);
        applyUiLanguage();
      });
    }

    function refreshInputTabSelectLabels() {
      const select = document.getElementById("inputTabSelect");
      if (!select) return;
      for (const option of select.options) {
        const config = inputTabs[option.value];
        if (config) option.textContent = t(config.labelKey);
      }
    }

    function recentKey(id) {
      return "linguinator_recent_" + id;
    }

    function legacyRecentKey(id) {
      return "nllb_recent_" + id;
    }

    function getRecent(id) {
      try {
        const value = localStorage.getItem(recentKey(id)) || localStorage.getItem(legacyRecentKey(id)) || "[]";
        return JSON.parse(value);
      } catch {
        return [];
      }
    }

    function saveRecent(id, code) {
      const recent = [code, ...getRecent(id).filter((item) => item !== code)].slice(0, 3);
      localStorage.setItem(recentKey(id), JSON.stringify(recent));
      saveRecentLanguage(code);
    }

    function recentLanguagesKey() {
      return "linguinator_recent_languages";
    }

    function getRecentLanguages() {
      try {
        return JSON.parse(localStorage.getItem(recentLanguagesKey()) || "[]");
      } catch {
        return [];
      }
    }

    function saveRecentLanguage(code) {
      const recent = [code, ...getRecentLanguages().filter((item) => item !== code)].slice(0, FAVORITE_LANGUAGE_COUNT * 2);
      localStorage.setItem(recentLanguagesKey(), JSON.stringify(recent));
    }

    function getFavoriteLanguages() {
      const favorites = getRecentLanguages().slice(0, FAVORITE_LANGUAGE_COUNT);
      for (const code of defaultFavoriteLanguages) {
        if (favorites.length >= FAVORITE_LANGUAGE_COUNT) break;
        if (!favorites.includes(code)) favorites.push(code);
      }
      return favorites;
    }

    function addOption(select, language) {
      const option = document.createElement("option");
      option.value = language.code;
      option.textContent = formatLanguageLabel(language.code);
      select.appendChild(option);
    }

    function countryFlag(countryCode) {
      if (!countryCode || countryCode === "UN") return "🌐";
      return countryCode
        .toUpperCase()
        .replace(/./g, (char) => String.fromCodePoint(127397 + char.charCodeAt(0)));
    }

    function titleCase(text) {
      return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
    }

    function languageDisplayName(languageCode) {
      const languagePart = languageCode.split("_")[0];
      let intlName = "";
      try {
        intlName = languageNames.of(languagePart) || "";
      } catch {
        intlName = "";
      }
      let name = languageFallbacks[languagePart] || intlName || languagePart;
      if (name === languagePart) {
        name = titleCase(languagePart);
      }
      const flag = countryFlag(languageCountries[languagePart]);
      return {name, flag};
    }

    function formatLanguageLabel(languageCode) {
      const display = languageDisplayName(languageCode);
      return (display.flag ? display.flag + " " : "") + display.name;
    }

    const inputTabs = {
      textarea: {
        panel: "textareaPanel",
        accept: "",
        labelKey: "textField",
        sourceFormat: "txt"
      },
      text: {
        panel: "filePanel",
        accept: ".txt,.html,.htm,.srt,.vtt,.json,.yaml,.yml,.po,.xlf,.xliff,text/plain,text/html,application/json,text/yaml,application/x-xliff+xml",
        labelKey: "textFile",
        sourceFormat: "txt"
      },
      markdown: {
        panel: "filePanel",
        accept: ".md,text/markdown,text/plain",
        labelKey: "markdownFile",
        sourceFormat: "md"
      },
      office: {
        panel: "filePanel",
        accept: ".docx,.odt,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.oasis.opendocument.text",
        labelKey: "officeDoc",
        sourceFormat: "md"
      },
      pptx: {
        panel: "filePanel",
        accept: ".pptx,application/vnd.openxmlformats-officedocument.presentationml.presentation",
        labelKey: "pptxFile",
        sourceFormat: "md"
      },
      csv: {
        panel: "filePanel",
        accept: ".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        labelKey: "csvFile",
        sourceFormat: "md"
      },
      pdf: {
        panel: "pdfPanel",
        accept: "application/pdf",
        labelKey: "pdf",
        sourceFormat: "pdf"
      }
    };

    function refreshInputLabels() {
      const config = inputTabs[currentInputTab] || inputTabs.textarea;
      const textFileLabel = document.getElementById("textFileLabel");
      if (textFileLabel) textFileLabel.textContent = t(config.labelKey);
    }

    function extractionPathForFile(fileName) {
      if (fileName.endsWith(".csv")) return "extract-csv";
      if (fileName.endsWith(".xlsx")) return "extract-xlsx";
      if (fileName.endsWith(".odt")) return "extract-odt";
      if (fileName.endsWith(".pptx")) return "extract-pptx";
      if (fileName.endsWith(".html") || fileName.endsWith(".htm")) return "extract-html";
      if (fileName.endsWith(".srt") || fileName.endsWith(".vtt")) return "extract-subtitle";
      if (fileName.endsWith(".json")) return "extract-json";
      if (fileName.endsWith(".yaml") || fileName.endsWith(".yml")) return "extract-yaml";
      if (fileName.endsWith(".po")) return "extract-po";
      if (fileName.endsWith(".xlf") || fileName.endsWith(".xliff")) return "extract-xliff";
      return "extract-docx";
    }

    function languageByCode(code) {
      return languageData.languages.find((language) => language.code === code);
    }

    function languageCodeOrFallback(code, fallback) {
      if (typeof code === "string" && code.trim()) {
        return code;
      }
      return fallback || "eng_Latn";
    }

    function setPickerValue(id, code) {
      const select = document.getElementById(id);
      const button = document.getElementById(id + "Button");
      const languageCode = languageCodeOrFallback(code, "eng_Latn");
      select.value = languageCode;
      button.textContent = formatLanguageLabel(languageCode);
      document.querySelectorAll("#" + id + "Menu .language-option").forEach((option) => {
        option.classList.toggle("active", option.dataset.code === languageCode);
      });
      closeLanguageMenus();
      updateModelQualityHint();
    }

    function updateModelQualityHint() {
      const hint = document.getElementById("modelQualityHint");
      if (!hint) return;
      const pairs = (languageData && languageData.dedicated_pairs) || [];
      const source = document.getElementById("source").value;
      const target = document.getElementById("target").value;
      const dedicated = pairs.some((pair) => pair[0] === source && pair[1] === target);
      hint.textContent = t(dedicated ? "modelDedicated" : "modelFallback");
    }

    function closeLanguageMenus() {
      document.querySelectorAll(".language-field.open").forEach((field) => {
        field.classList.remove("open");
        const menu = field.querySelector(".language-menu");
        const search = field.querySelector(".language-search");
        if (search) search.value = "";
        if (menu) filterLanguageMenu(menu, "");
      });
    }

    function addLanguageMenuOption(menu, id, language, selectedValue) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "language-option";
      option.dataset.code = language.code;
      option.title = "Select " + formatLanguageLabel(language.code) + ".";
      option.innerHTML =
        '<span class="language-name">' + escapeHtml(formatLanguageLabel(language.code)) + '</span>' +
        '<span class="language-code">' + escapeHtml(language.code) + '</span>';
      option.classList.toggle("active", language.code === selectedValue);
      option.addEventListener("click", () => setPickerValue(id, language.code));
      menu.appendChild(option);
    }

    function addLanguageGroup(menu, title) {
      const group = document.createElement("div");
      group.className = "language-group";
      group.textContent = title;
      menu.appendChild(group);
      return group;
    }

    function renderSelect(id, selectedValue) {
      const select = document.getElementById(id);
      const menu = document.getElementById(id + "Menu");
      const favoriteCodes = getFavoriteLanguages();
      const favorites = favoriteCodes.map(languageByCode).filter(Boolean);
      select.innerHTML = "";
      menu.innerHTML = "";

      for (const language of languageData.languages) {
        addOption(select, language);
      }

      const search = document.createElement("input");
      search.type = "text";
      search.className = "language-search";
      search.placeholder = t("searchLanguage");
      search.title = t("searchLanguage");
      search.addEventListener("click", (event) => event.stopPropagation());
      search.addEventListener("input", () => filterLanguageMenu(menu, search.value));
      menu.appendChild(search);

      addLanguageGroup(menu, t("favorites"));
      for (const language of favorites) {
        addLanguageMenuOption(menu, id, language, selectedValue);
      }

      const divider = document.createElement("div");
      divider.className = "language-divider";
      menu.appendChild(divider);
      addLanguageGroup(menu, t("allLanguages"));
      for (const language of languageData.languages) {
        if (!favoriteCodes.includes(language.code)) {
          addLanguageMenuOption(menu, id, language, selectedValue);
        }
      }

      setPickerValue(id, selectedValue);
    }

    function filterLanguageMenu(menu, query) {
      const normalized = query.trim().toLowerCase();
      const groups = [];
      let currentGroup = null;
      let currentGroupHasMatch = false;
      for (const child of menu.children) {
        if (child.classList.contains("language-search")) continue;
        if (child.classList.contains("language-divider")) {
          child.classList.toggle("hidden", Boolean(normalized));
          continue;
        }
        if (child.classList.contains("language-group")) {
          if (currentGroup) groups.push({el: currentGroup, hasMatch: currentGroupHasMatch});
          currentGroup = child;
          currentGroupHasMatch = false;
          continue;
        }
        const matches = !normalized ||
          child.dataset.code.toLowerCase().includes(normalized) ||
          child.textContent.toLowerCase().includes(normalized);
        child.classList.toggle("hidden", !matches);
        if (matches) currentGroupHasMatch = true;
      }
      if (currentGroup) groups.push({el: currentGroup, hasMatch: currentGroupHasMatch});
      for (const group of groups) {
        group.el.classList.toggle("hidden", !group.hasMatch);
      }
    }

    function setupLanguagePicker(id) {
      const field = document.querySelector('[data-picker="' + id + '"]');
      const button = document.getElementById(id + "Button");
      button.addEventListener("click", () => {
        const wasOpen = field.classList.contains("open");
        closeLanguageMenus();
        field.classList.toggle("open", !wasOpen);
        if (!wasOpen) {
          const search = field.querySelector(".language-search");
          if (search) search.focus();
        }
      });
    }

    async function loadLanguages() {
      const response = await fetch("languages");
      if (!response.ok) {
        throw new Error("Could not load languages: " + response.status);
      }
      languageData = await response.json();
      if (!languageData || !Array.isArray(languageData.languages) || !languageData.languages.length) {
        throw new Error("Language response was empty.");
      }
      const sourceFallback = languageCodeOrFallback(languageData.source_default, "eng_Latn");
      const targetFallback = languageCodeOrFallback(languageData.target_default, "deu_Latn");
      renderSelect("source", languageCodeOrFallback(getRecent("source")[0], sourceFallback));
      renderSelect("target", languageCodeOrFallback(getRecent("target")[0], targetFallback));
    }

    setupLanguagePicker("source");
    setupLanguagePicker("target");
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".language-field")) {
        closeLanguageMenus();
      }
    });

    function setInputTab(tab) {
      const config = inputTabs[tab] || inputTabs.textarea;
      currentInputTab = tab;
      const inputTabSelect = document.getElementById("inputTabSelect");
      if (inputTabSelect) inputTabSelect.value = currentInputTab;
      currentSourceFormat = config.sourceFormat;
      currentOriginalExtension = config.sourceFormat;
      document.querySelectorAll("[data-input-tab]").forEach((button) => {
        button.classList.toggle("active", button.dataset.inputTab === tab);
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === config.panel);
      });
      if (config.panel === "filePanel") {
        const fileInput = document.getElementById("textFile");
        fileInput.accept = config.accept;
        fileInput.value = "";
        document.getElementById("textFileLabel").textContent = t(config.labelKey);
      }
      const showSheet = tab === "csv";
      document.querySelectorAll(".file-extra").forEach((input) => {
        input.classList.toggle("visible", showSheet);
      });
      updateDownloadButtons();
    }

    document.querySelectorAll("[data-input-tab]").forEach((button) => {
      button.addEventListener("click", () => setInputTab(button.dataset.inputTab));
    });

    document.getElementById("inputTabSelect").addEventListener("change", (event) => {
      setInputTab(event.target.value);
    });

    function setResult(text) {
      fullResultText = text;
      updateDownloadButtons();
    }

    function clearCurrentWork() {
      document.getElementById("text").value = "";
      document.getElementById("textFile").value = "";
      document.getElementById("pdf").value = "";
      document.getElementById("pageRange").value = "";
      document.getElementById("sheetName").value = "";
      document.getElementById("csvColumns").value = "";
      document.getElementById("pdfPlaintext").checked = false;
      currentSourceFormat = (inputTabs[currentInputTab] || inputTabs.textarea).sourceFormat;
      currentOriginalExtension = currentSourceFormat;
      updateCounter();
      setResult("");
      lastCompletedJob = null;
      clearProgress();
    }

    function outputFormatDetails(format) {
      if (format === "original") {
        if (originalExportExtensions.includes(currentOriginalExtension)) {
          return {extension: currentOriginalExtension, originalFile: true};
        }
        if (currentSourceFormat === "pdf") return {extension: "pdf", contentType: "application/pdf", pdf: true};
        if (currentSourceFormat === "md") return {extension: "md", contentType: "text/markdown"};
        return {extension: "txt", contentType: "text/plain"};
      }
      if (format === "pdf") return {extension: "pdf", contentType: "application/pdf", pdf: true};
      if (format === "md") return {extension: "md", contentType: "text/markdown"};
      return {extension: "txt", contentType: "text/plain"};
    }

    function canUseOriginalFormat() {
      return currentSourceFormat === "pdf" ||
        currentSourceFormat === "md" ||
        originalExportExtensions.includes(currentOriginalExtension);
    }

    function updateDownloadButtons() {
      const hasText = Boolean(fullResultText.trim());
      const formats = {txt: true, md: true, pdf: true, original: canUseOriginalFormat()};
      for (const format of Object.keys(formats)) {
        const button = document.getElementById("download" + format.charAt(0).toUpperCase() + format.slice(1));
        button.classList.toggle("hidden", !formats[format]);
        button.disabled = !hasText;
        button.textContent = format === "original"
          ? t(outputFormatKeys[format]) + " (." + outputFormatDetails(format).extension + ")"
          : t(outputFormatKeys[format]);
      }
    }

    async function downloadFormat(format) {
      const text = fullResultText;
      if (!text.trim()) return;
      const details = outputFormatDetails(format);
      if (details.originalFile) {
        await downloadOriginalFile(text, details);
        return;
      }
      if (details.pdf) {
        if (currentSourceFormat === "pdf" && lastCompletedJob && lastCompletedJob.kind === "translate-pdf-layout" && lastCompletedJob.history_id) {
          window.location.href = "history/" + lastCompletedJob.history_id + "/export?format=original";
          return;
        }
        const response = await fetch("export-pdf", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({text})
        });
        if (!response.ok) {
          const error = await response.text();
          showProgress("failed", 0, errorTextFromResponse(error));
          return;
        }
        saveBlob(await response.blob(), details.extension);
        return;
      }
      saveBlob(new Blob([text], {type: details.contentType + ";charset=utf-8"}), details.extension);
    }

    document.querySelectorAll("#downloadActions button[data-format]").forEach((button) => {
      button.addEventListener("click", () => downloadFormat(button.dataset.format));
    });

    function saveBlob(blob, extension) {
      const link = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = "linguinator-result-" + stamp + "." + extension;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }

    function originalExportPath(extension) {
      return {
        docx: "export-docx",
        odt: "export-odt",
        pptx: "export-pptx",
        csv: "export-csv",
        xlsx: "export-xlsx",
        html: "export-html",
        htm: "export-html",
        srt: "export-subtitle",
        vtt: "export-subtitle",
        json: "export-json",
        yaml: "export-yaml",
        yml: "export-yaml",
        po: "export-po",
        xlf: "export-xliff",
        xliff: "export-xliff"
      }[extension];
    }

    async function downloadOriginalFile(text, details) {
      const file = document.getElementById("textFile").files[0];
      const path = originalExportPath(details.extension);
      if (!file || !path) {
        showProgress("failed", 0, t("originalNeedsFile"));
        return;
      }
      const form = new FormData();
      form.append("file", file);
      form.append("text", text);
      if (details.extension === "csv" || details.extension === "xlsx") {
        form.append("columns", document.getElementById("csvColumns").value);
      }
      if (details.extension === "xlsx") {
        form.append("sheet_name", document.getElementById("sheetName").value);
      }
      const response = await fetch(path, {method: "POST", body: form});
      if (!response.ok) {
        const error = await response.text();
        showProgress("failed", 0, errorTextFromResponse(error));
        return;
      }
      saveBlob(await response.blob(), details.extension);
    }

    async function loadTextFile() {
      const file = document.getElementById("textFile").files[0];
      if (!file) {
        showProgress("failed", 0, t("selectFileFirst"));
        return;
      }
      const lowerName = file.name.toLowerCase();
      const isCsv = lowerName.endsWith(".csv");
      const isXlsx = lowerName.endsWith(".xlsx");
      const isOfficeFile = lowerName.endsWith(".docx") || lowerName.endsWith(".odt") || lowerName.endsWith(".pptx");
      const isStructuredText = [".html", ".htm", ".srt", ".vtt", ".json", ".yaml", ".yml", ".po", ".xlf", ".xliff"].some((extension) => lowerName.endsWith(extension));
      currentOriginalExtension = lowerName.split(".").pop() || currentOriginalExtension;
      if (isCsv || isXlsx || isOfficeFile || isStructuredText) {
        const form = new FormData();
        form.append("file", file);
        if (isCsv || isXlsx) {
          form.append("columns", document.getElementById("csvColumns").value);
        }
        if (isXlsx) {
          form.append("sheet_name", document.getElementById("sheetName").value);
        }
        const path = extractionPathForFile(lowerName);
        showProgress("extracting", 0, t("extracting") + " " + lowerName.split(".").pop().toUpperCase() + "...");
        const response = await fetch(path, {method: "POST", body: form});
        const text = await response.text();
        if (!response.ok) {
          showProgress("failed", 0, errorTextFromResponse(text));
          return;
        }
        document.getElementById("text").value = text;
        currentSourceFormat = isCsv || isXlsx || isOfficeFile || isStructuredText ? "md" : currentSourceFormat;
      } else {
        document.getElementById("text").value = await file.text();
        currentSourceFormat = lowerName.endsWith(".md") ? "md" : "txt";
        currentOriginalExtension = currentSourceFormat;
      }
      updateCounter();
      setResult("");
      clearProgress();
    }

    document.getElementById("loadTextFile").addEventListener("click", loadTextFile);
    document.getElementById("clearInput").addEventListener("click", clearCurrentWork);

    function errorTextFromResponse(text) {
      try {
        const data = JSON.parse(text);
        return data.detail || text;
      } catch {
        return text;
      }
    }

    function escapeHtml(text) {
      return String(text).replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function showProgress(status, percent, info, position, jobLabel) {
      const progress = document.getElementById("progress");
      const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
      const titleStatus = status.charAt(0).toUpperCase() + status.slice(1);
      let prefix;
      if ((status === "queued" || status === "running") && position && position > 0) {
        prefix = "#" + position + " " + titleStatus;
      } else if (status === "extracting") {
        prefix = titleStatus;
      } else {
        prefix = safePercent + "% " + titleStatus;
      }
      document.title = prefix + (jobLabel ? " - " + jobLabel : "") + " - " + baseTitle;
      progress.className = "progress " + status;
      progress.innerHTML =
        '<div class="progress-top">' +
          '<span class="progress-status">' + escapeHtml(status) + '</span>' +
          '<span class="progress-info">' + escapeHtml(info) + '</span>' +
        '</div>' +
        '<div class="progress-track"><div class="progress-fill" style="width: ' + safePercent + '%"></div></div>';
    }

    function clearProgress() {
      const progress = document.getElementById("progress");
      document.title = baseTitle;
      progress.className = "progress";
      progress.innerHTML = "";
    }

    async function loadHealth() {
      const response = await fetch("health");
      const data = await response.json();
      maxChars = data.max_chars || 0;
      updateCounter();
    }

    function updateCounter() {
      const counter = document.getElementById("counter");
      const length = document.getElementById("text").value.length;
      const chunks = maxChars > 0 ? Math.max(1, Math.ceil(length / maxChars)) : 1;
      counter.textContent = length + " / " + maxChars + " (" + chunks + " " + t("chunks") + ")";
      counter.title = length + " characters used out of " + maxChars + ". Estimated translation chunks: " + chunks + ".";
      counter.classList.toggle("over", maxChars > 0 && length > maxChars);
    }

    document.getElementById("text").addEventListener("input", updateCounter);

    let audioContext = null;

    function ensureAudioContext() {
      if (audioContext) return audioContext;
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return null;
      audioContext = new AudioContextClass();
      return audioContext;
    }

    function playNotificationSound() {
      const context = audioContext;
      if (!context) return;
      const now = context.currentTime;
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(880, now);
      oscillator.frequency.setValueAtTime(1175, now + .12);
      gain.gain.setValueAtTime(0, now);
      gain.gain.linearRampToValueAtTime(.2, now + .01);
      gain.gain.linearRampToValueAtTime(0, now + .3);
      oscillator.connect(gain);
      gain.connect(context.destination);
      oscillator.start(now);
      oscillator.stop(now + .3);
    }

    function formatEta(seconds) {
      if (seconds === null || seconds === undefined) return "calculating";
      if (seconds < 60) return seconds + "s";
      const minutes = Math.floor(seconds / 60);
      const rest = seconds % 60;
      return minutes + "m " + rest + "s";
    }

    function formatJobTime(timestamp) {
      if (!timestamp) return "";
      return new Date(timestamp * 1000).toLocaleString();
    }

    async function controlJob(jobId, action) {
      const response = await fetch("jobs/" + jobId + "/" + action, {method: "POST"});
      if (!response.ok) {
        const text = await response.text();
        showProgress("failed", 0, errorTextFromResponse(text));
        return null;
      }
      const job = await response.json();
      await loadQueue();
      return job;
    }

    function queueActionButton(job, action, label, enabledStatuses) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = label;
      button.title = label;
      button.disabled = !enabledStatuses.includes(job.status);
      button.addEventListener("click", async () => {
        const updated = await controlJob(job.id, action);
        if (updated && activeJobId === job.id) {
          updateProgress(updated);
        }
      });
      return button;
    }

    function queuePauseResumeButton(job) {
      const isPaused = job.status === "paused";
      return queueActionButton(job, isPaused ? "resume" : "pause", isPaused ? t("resume") : t("pause"), ["queued", "running", "paused"]);
    }

    async function loadQueue() {
      const response = await fetch("jobs");
      if (!response.ok) return;
      const data = await response.json();
      let hasNewlyCompleted = false;
      for (const job of data.items) {
        if (job.status === "complete" && !seenCompletedJobIds.has(job.id)) {
          seenCompletedJobIds.add(job.id);
          hasNewlyCompleted = true;
        }
      }
      if (hasNewlyCompleted) loadHistory();
      const queue = document.getElementById("queue");
      const workers = document.getElementById("queueWorkers");
      queue.innerHTML = "";
      workers.textContent = data.workers + " " + t(data.workers === 1 ? "workerSingular" : "workerPlural");
      const visibleItems = data.items.filter((job) => !["complete", "failed", "cancelled"].includes(job.status));
      updateQueueControlButtons(visibleItems);
      if (!visibleItems.length) {
        queue.textContent = t("noQueuedJobs");
        return;
      }
      for (const job of visibleItems) {
        const row = document.createElement("div");
        row.className = "queue-row";
        const main = document.createElement("div");
        const title = document.createElement("div");
        title.className = "queue-title";
        const typeBadge = document.createElement("span");
        typeBadge.className = "queue-type-badge";
        typeBadge.textContent = (job.source_extension || "txt").toUpperCase();
        const position = job.position ? "#" + job.position + " " : "";
        title.appendChild(typeBadge);
        title.appendChild(document.createTextNode(position + (job.label || job.kind) + " - " + job.status));
        const meta = document.createElement("div");
        meta.className = "queue-meta";
        const languages = [job.source, job.target].filter(Boolean).join(" -> ");
        const started = job.started_at ? t("started") + " " + formatJobTime(job.started_at) : t("queued") + " " + formatJobTime(job.queued_at);
        meta.textContent = [languages, started].filter(Boolean).join(" | ");
        const progress = document.createElement("div");
        progress.className = "queue-progress";
        const progressLabel = job.status === "queued" && job.position
          ? t("queuePosition", {position: job.position})
          : (job.percent || 0) + "%";
        progress.textContent = progressLabel + " | " + (job.current || 0) + " / " + (job.total || 0) + " " + t("chunks") + " | " + (job.message || "");
        const actions = document.createElement("div");
        actions.className = "queue-actions";
        actions.appendChild(queuePauseResumeButton(job));
        actions.appendChild(queueActionButton(job, "cancel", t("skip"), ["queued", "running", "paused"]));
        main.appendChild(title);
        main.appendChild(meta);
        main.appendChild(progress);
        row.appendChild(main);
        row.appendChild(actions);
        row.classList.toggle("watched", job.id === activeJobId);
        row.title = t("watchJob");
        row.addEventListener("click", (event) => {
          if (event.target.closest(".queue-actions")) return;
          watchJob(job.id);
        });
        queue.appendChild(row);
      }
    }

    function updateProgress(job) {
      const current = job.current || 0;
      const total = job.total || 0;
      const percent = job.percent || 0;
      const position = job.position && job.position > 0 ? job.position : undefined;
      const eta = job.status === "running" ? "ETA " + formatEta(job.eta_seconds) : "";
      const progressLabel = job.status === "queued" && position
        ? t("queuePosition", {position})
        : percent + "%";
      showProgress(
        job.status,
        percent,
        progressLabel + " | " + current + " / " + total + " chunks | " + eta + " " + (job.message || ""),
        position,
        job.label || job.kind
      );
    }

    const QUEUE_CONTROL_STATUSES = {
      pause: ["queued", "running"],
      resume: ["paused"],
      cancel: ["queued", "running", "paused"],
    };

    function updateQueueControlButtons(items) {
      document.getElementById("pauseJob").disabled = !items.some((job) => QUEUE_CONTROL_STATUSES.pause.includes(job.status));
      document.getElementById("resumeJob").disabled = !items.some((job) => QUEUE_CONTROL_STATUSES.resume.includes(job.status));
      document.getElementById("stopJob").disabled = !items.some((job) => QUEUE_CONTROL_STATUSES.cancel.includes(job.status));
    }

    let pollToken = 0;

    function watchJob(jobId) {
      ensureAudioContext();
      activeJobId = jobId;
      const token = ++pollToken;
      loadQueue().catch(() => {});
      pollJob(jobId, token);
    }

    async function pollJob(jobId, token) {
      while (token === pollToken) {
        const response = await fetch("jobs/" + jobId);
        if (!response.ok) {
          const text = await response.text();
          if (token === pollToken) {
            showProgress("failed", 0, errorTextFromResponse(text));
            activeJobId = null;
          }
          return;
        }
        const job = await response.json();
        if (token !== pollToken) return;
        updateProgress(job);
        if (job.status === "complete") {
          setResult(job.result || "");
          lastCompletedJob = job;
          loadHistory();
          loadQueue();
          playNotificationSound();
          activeJobId = null;
          return;
        }
        if (job.status === "cancelled") {
          setResult("");
          loadQueue();
          activeJobId = null;
          return;
        }
        if (job.status === "failed") {
          showProgress("failed", job.percent || 0, job.error || t("jobFailed"));
          setResult("");
          loadQueue();
          playNotificationSound();
          activeJobId = null;
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1500));
      }
    }

    async function startTextJob() {
      const source = document.getElementById("source").value;
      const target = document.getElementById("target").value;
      const length = document.getElementById("text").value.length;
      const chunks = maxChars > 0 ? Math.max(1, Math.ceil(length / maxChars)) : 1;
      const sourceFile = currentInputTab !== "textarea" ? document.getElementById("textFile").files[0] : null;
      saveRecent("source", source);
      saveRecent("target", target);
      renderSelect("source", source);
      renderSelect("target", target);
      if (currentInputTab === "textarea") {
        currentSourceFormat = "txt";
        currentOriginalExtension = "txt";
      }
      setResult("");
      lastCompletedJob = null;
      showProgress("queued", 0, chunks > 1 ? t("startingChunks", {count: chunks}) : t("starting"));
      let response;
      if (sourceFile) {
        const form = new FormData();
        form.append("file", sourceFile);
        form.append("text", document.getElementById("text").value);
        form.append("source", source);
        form.append("target", target);
        form.append("columns", document.getElementById("csvColumns").value);
        form.append("sheet_name", document.getElementById("sheetName").value);
        response = await fetch("jobs/translate-file", {method: "POST", body: form});
      } else {
        response = await fetch("jobs/translate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            q: document.getElementById("text").value,
            source: source,
            target: target
          })
        });
      }
      if (!response.ok) {
        const text = await response.text();
        showProgress("failed", 0, errorTextFromResponse(text));
        setResult("");
        return;
      }
      const data = await response.json();
      ensureAudioContext();
      activeJobId = data.job_id;
      const token = ++pollToken;
      loadQueue();
      await pollJob(data.job_id, token);
    }

    async function startCurrentJob() {
      if (currentInputTab === "pdf") {
        await postPdfJob();
        return;
      }
      await startTextJob();
    }

    document.getElementById("translate").addEventListener("click", startCurrentJob);

    async function postPdfJob() {
      const file = document.getElementById("pdf").files[0];
      if (!file) {
        showProgress("failed", 0, t("selectPdfFirst"));
        return;
      }
      const source = document.getElementById("source").value;
      const target = document.getElementById("target").value;
      saveRecent("source", source);
      saveRecent("target", target);
      renderSelect("source", source);
      renderSelect("target", target);
      currentSourceFormat = "pdf";
      const layoutMode = !document.getElementById("pdfPlaintext").checked;
      const form = new FormData();
      form.append("file", file);
      form.append("source", source);
      form.append("target", target);
      if (!layoutMode) {
        form.append("page_range", document.getElementById("pageRange").value);
      }
      setResult("");
      lastCompletedJob = null;
      showProgress("extracting", 0, t("uploadingPdf"));
      const response = await fetch(layoutMode ? "jobs/translate-pdf-layout" : "jobs/translate-pdf", {method: "POST", body: form});
      if (!response.ok) {
        const text = await response.text();
        showProgress("failed", 0, errorTextFromResponse(text));
        setResult("");
        return;
      }
      const data = await response.json();
      ensureAudioContext();
      activeJobId = data.job_id;
      const token = ++pollToken;
      loadQueue();
      await pollJob(data.job_id, token);
    }

    async function controlQueue(action) {
      const response = await fetch("jobs");
      if (!response.ok) return;
      const data = await response.json();
      const eligible = data.items.filter((job) => (QUEUE_CONTROL_STATUSES[action] || []).includes(job.status));
      await Promise.all(eligible.map((job) => controlJob(job.id, action)));
      if (activeJobId) {
        const activeResponse = await fetch("jobs/" + activeJobId);
        if (activeResponse.ok) updateProgress(await activeResponse.json());
      }
    }

    function formatBytes(bytes) {
      if (!bytes) return "0 B";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
      return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    async function deleteHistoryItem(id) {
      const response = await fetch("history/" + id, {method: "DELETE"});
      if (!response.ok) {
        const text = await response.text();
        showProgress("failed", 0, errorTextFromResponse(text));
        return;
      }
      await loadHistory();
    }

    async function loadHistory() {
      const response = await fetch("history");
      const data = await response.json();
      historyItems = data.items || [];
      historyVisibleCount = Math.min(historyVisibleCount || HISTORY_PAGE_SIZE, Math.max(historyItems.length, HISTORY_PAGE_SIZE));
      renderHistory();
    }

    function renderHistory() {
      const history = document.getElementById("history");
      const loadMore = document.getElementById("loadMoreHistory");
      history.innerHTML = "";
      if (!historyItems.length) {
        history.textContent = t("noHistory");
        loadMore.classList.add("hidden");
        return;
      }
      const visibleItems = historyItems.slice(0, historyVisibleCount);
      for (const item of visibleItems) {
        const row = document.createElement("div");
        row.className = "history-row";
        const main = document.createElement("div");
        main.className = "history-main";
        const link = document.createElement("a");
        link.href = "history/" + item.id;
        link.textContent = item.filename;
        link.download = item.filename;
        const meta = document.createElement("div");
        meta.className = "history-meta";
        meta.textContent = item.source + " -> " + item.target + " | " + formatBytes(item.size_bytes) + " | " + item.created_at;
        const format = document.createElement("select");
        format.className = "history-format";
        format.title = "Select the history download format.";
        const historyFormats = ["md", "txt", "pdf"];
        if (item.has_source_file && item.source_extension) {
          historyFormats.push("original");
        }
        for (const optionFormat of historyFormats) {
          const option = document.createElement("option");
          option.value = optionFormat;
          option.textContent = optionFormat === "original"
            ? t("formatOriginal") + " (." + item.source_extension + ")"
            : t(outputFormatKeys[optionFormat]);
          format.appendChild(option);
        }
        const download = document.createElement("a");
        download.className = "history-download secondary-link";
        download.href = "history/" + item.id;
        download.textContent = t("download");
        download.download = item.filename;
        download.title = "Download this history item.";
        format.addEventListener("change", () => {
          download.href = "history/" + item.id + "/export?format=" + encodeURIComponent(format.value);
          download.download = "";
        });
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "secondary history-delete";
        remove.textContent = t("delete");
        remove.title = "Delete this history item.";
        remove.addEventListener("click", () => deleteHistoryItem(item.id));
        main.appendChild(link);
        main.appendChild(meta);
        const actions = document.createElement("div");
        actions.className = "history-actions";
        actions.appendChild(format);
        actions.appendChild(download);
        actions.appendChild(remove);
        row.appendChild(main);
        row.appendChild(actions);
        history.appendChild(row);
      }
      loadMore.textContent = t("loadMore");
      loadMore.title = t("loadMore");
      loadMore.classList.toggle("hidden", historyVisibleCount >= historyItems.length);
    }

    document.getElementById("loadMoreHistory").addEventListener("click", () => {
      historyVisibleCount += HISTORY_PAGE_SIZE;
      renderHistory();
    });

    function setHistoryCollapsed(collapsed) {
      const body = document.getElementById("historyBody");
      const icon = document.getElementById("historyCollapseIcon");
      body.classList.toggle("hidden", collapsed);
      icon.classList.toggle("collapsed", collapsed);
      localStorage.setItem("linguinator_history_collapsed", collapsed ? "1" : "0");
    }

    document.getElementById("historyCollapse").addEventListener("click", () => {
      const body = document.getElementById("historyBody");
      setHistoryCollapsed(!body.classList.contains("hidden"));
    });

    setHistoryCollapsed(localStorage.getItem("linguinator_history_collapsed") === "1");

    document.getElementById("pauseJob").addEventListener("click", () => controlQueue("pause"));
    document.getElementById("resumeJob").addEventListener("click", () => controlQueue("resume"));
    document.getElementById("stopJob").addEventListener("click", () => controlQueue("cancel"));

    setupUiLanguagePicker();
    loadLanguages().catch((error) => {
      showProgress("failed", 0, error.toString());
    });
    loadHealth().catch((error) => {
      showProgress("failed", 0, error.toString());
    });
    loadHistory().catch((error) => {
      document.getElementById("history").textContent = error.toString();
    });
    loadQueue().catch(() => {});
    setInterval(() => {
      loadQueue().catch(() => {});
    }, 3000);
    setInputTab("pdf");
    applyUiLanguage();
