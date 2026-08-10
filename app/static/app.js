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
    let historyPage = 0;
    let historyFilterText = "";
    let historyTimezone = "UTC";
    let timeFormat = "auto";
    const seenCompletedJobIds = new Set();
    const HISTORY_PAGE_SIZE = 5;
    const baseTitle = document.title || "Linguinator";
    const outputFormatKeys = {txt: "formatTxt", md: "formatMarkdown", pdf: "formatPdf", doc: "formatDoc", original: "formatOriginal"};
    const uiText = {
      en: {
        uiLanguage: "UI Language",
        subtitle: "Local translation workbench for text and document workflows.",
        queue: "Queue",
        source: "Source",
        target: "Target",
        searchLanguage: "Search language...",
        detection: "Detection",
        favorites: "Favorites",
        allLanguages: "All languages",
        loading: "Loading...",
        textField: "Text Field",
        text: "Text",
        markdown: "Markdown",
        officeDoc: "DOC File",
        pptxFile: "PowerPoint",
        csvFile: "CSV File",
        pdf: "PDF",
        website: "Website",
        websiteHint: "Reads the article out of a public page - no login, no paywall - and translates it. Download as PDF, Word or text.",
        fetchingPage: "Fetching page...",
        websiteInvalid: "Enter a public page address starting with http:// or https://",
        textFile: "Text File",
        markdownFile: "Markdown File",
        loadFile: "Load File",
        translateInput: "Translate Input",
        clear: "Clear",
        pause: "Pause",
        resume: "Resume",
        stop: "Stop",
        skip: "Skip",
        history: "History",
        noQueuedJobs: "No queued jobs.",
        noHistory: "No saved translations yet.",
        noHistoryMatch: "No history entries match this filter.",
        pageInfo: "Page {page} / {total}",
        queued: "Queued",
        queuePosition: "Queue position #{position}",
        watchJob: "Click to track this job in the progress bar and tab title.",
        started: "Started",
        chunks: "chunks",
        jobFailed: "Job failed.",
        selectFileFirst: "Select a supported text, document, table, subtitle, or localization file first.",
        selectPdfFirst: "Select a PDF first.",
        extracting: "Extracting",
        starting: "Starting...",
        startingChunks: "Starting {count} chunks...",
        uploadingPdf: "Uploading PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "Plain PDF",
        formatDoc: "Plain Doc",
        formatOriginal: "Original Format",
        modelDedicated: "Dedicated model for this language pair.",
        modelFallback: "No dedicated model for this pair, using the multilingual fallback.",
        modelAutoDetect: "Source language will be detected automatically.",
        autoDetect: "Auto-detect",
        ownJobDone: "Your job is done:"
      },
      de: {
        uiLanguage: "UI-Sprache",
        subtitle: "Lokale Uebersetzungsoberflaeche fuer Text- und Dokument-Workflows.",
        queue: "Warteschlange",
        source: "Quelle",
        target: "Ziel",
        searchLanguage: "Sprache suchen...",
        detection: "Erkennung",
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
        website: "Webseite",
        websiteHint: "Holt den Artikel aus einer oeffentlich erreichbaren Seite - ohne Login, ohne Paywall - und uebersetzt ihn. Download als PDF, Word oder Text.",
        fetchingPage: "Hole Seite...",
        websiteInvalid: "Trage eine oeffentlich erreichbare Adresse ein, die mit http:// oder https:// beginnt.",
        textFile: "Textdatei",
        markdownFile: "Markdown-Datei",
        loadFile: "Datei laden",
        translateInput: "Eingabe uebersetzen",
        clear: "Leeren",
        pause: "Pause",
        resume: "Fortsetzen",
        stop: "Stoppen",
        skip: "Ueberspringen",
        history: "History",
        noQueuedJobs: "Keine wartenden Jobs.",
        noHistory: "Noch keine gespeicherten Uebersetzungen.",
        noHistoryMatch: "Kein History-Eintrag passt zu diesem Filter.",
        pageInfo: "Seite {page} / {total}",
        queued: "Eingereiht",
        queuePosition: "Warteschlangenposition #{position}",
        watchJob: "Klicken, um diesen Job im Fortschrittsbalken und Tab-Titel zu verfolgen.",
        started: "Gestartet",
        chunks: "Chunks",
        jobFailed: "Job fehlgeschlagen.",
        selectFileFirst: "Waehle zuerst eine unterstuetzte Text-, Dokument-, Tabellen-, Untertitel- oder Lokalisierungsdatei.",
        selectPdfFirst: "Waehle zuerst eine PDF aus.",
        extracting: "Extrahiere",
        starting: "Starte...",
        startingChunks: "Starte {count} Chunks...",
        uploadingPdf: "Lade PDF hoch...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "Nur Text-PDF",
        formatDoc: "Nur Text-Doc",
        formatOriginal: "Originalformat",
        modelDedicated: "Eigenes Modell fuer dieses Sprachpaar.",
        modelFallback: "Kein eigenes Modell fuer dieses Paar, nutzt den mehrsprachigen Fallback.",
        modelAutoDetect: "Quellsprache wird automatisch erkannt.",
        autoDetect: "Automatisch erkennen",
        ownJobDone: "Dein Job ist fertig:"
      },
      es: {
        uiLanguage: "Idioma de UI",
        subtitle: "Banco local de traduccion para flujos de texto y documentos.",
        queue: "Cola",
        source: "Origen",
        target: "Destino",
        searchLanguage: "Buscar idioma...",
        detection: "Deteccion",
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
        website: "Sitio web",
        websiteHint: "Extrae el articulo de una pagina publica - sin inicio de sesion ni muro de pago - y lo traduce. Descarga en PDF, Word o texto.",
        fetchingPage: "Obteniendo pagina...",
        websiteInvalid: "Introduce una direccion publica que empiece por http:// o https://",
        textFile: "Archivo de texto",
        markdownFile: "Archivo Markdown",
        loadFile: "Cargar archivo",
        translateInput: "Traducir entrada",
        clear: "Limpiar",
        pause: "Pausar",
        resume: "Continuar",
        stop: "Detener",
        skip: "Omitir",
        history: "Historial",
        noQueuedJobs: "No hay trabajos en cola.",
        noHistory: "Aun no hay traducciones guardadas.",
        noHistoryMatch: "Ningun elemento del historial coincide con este filtro.",
        pageInfo: "Pagina {page} / {total}",
        queued: "En cola",
        queuePosition: "Posicion en cola #{position}",
        watchJob: "Haz clic para seguir este trabajo en la barra de progreso y el titulo de la pestana.",
        started: "Iniciado",
        chunks: "fragmentos",
        jobFailed: "El trabajo fallo.",
        selectFileFirst: "Selecciona primero un archivo compatible de texto, documento, tabla, subtitulos o localizacion.",
        selectPdfFirst: "Selecciona primero un PDF.",
        extracting: "Extrayendo",
        starting: "Iniciando...",
        startingChunks: "Iniciando {count} fragmentos...",
        uploadingPdf: "Subiendo PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF simple",
        formatDoc: "Doc simple",
        formatOriginal: "Formato original",
        modelDedicated: "Modelo dedicado para este par de idiomas.",
        modelFallback: "Sin modelo dedicado para este par, se usa el alternativo multilingue.",
        modelAutoDetect: "El idioma de origen se detectara automaticamente.",
        autoDetect: "Deteccion automatica",
        ownJobDone: "Tu trabajo esta listo:"
      },
      fr: {
        uiLanguage: "Langue UI",
        subtitle: "Atelier local de traduction pour les workflows texte et documents.",
        queue: "File d'attente",
        source: "Source",
        target: "Cible",
        searchLanguage: "Rechercher une langue...",
        detection: "Detection",
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
        website: "Site web",
        websiteHint: "Recupere l'article d'une page publique - sans connexion ni paywall - et le traduit. Telechargement en PDF, Word ou texte.",
        fetchingPage: "Recuperation de la page...",
        websiteInvalid: "Saisis une adresse publique commencant par http:// ou https://",
        textFile: "Fichier texte",
        markdownFile: "Fichier Markdown",
        loadFile: "Charger le fichier",
        translateInput: "Traduire l'entree",
        clear: "Effacer",
        pause: "Pause",
        resume: "Reprendre",
        stop: "Arreter",
        skip: "Passer",
        history: "Historique",
        noQueuedJobs: "Aucun job en file.",
        noHistory: "Aucune traduction enregistree.",
        noHistoryMatch: "Aucun element de l'historique ne correspond a ce filtre.",
        pageInfo: "Page {page} / {total}",
        queued: "En file",
        queuePosition: "Position en file #{position}",
        watchJob: "Cliquer pour suivre ce job dans la barre de progression et le titre de l'onglet.",
        started: "Demarre",
        chunks: "segments",
        jobFailed: "Le job a echoue.",
        selectFileFirst: "Selectionne d'abord un fichier compatible texte, document, tableau, sous-titres ou localisation.",
        selectPdfFirst: "Selectionne d'abord un PDF.",
        extracting: "Extraction",
        starting: "Demarrage...",
        startingChunks: "Demarrage de {count} segments...",
        uploadingPdf: "Televersement du PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF simple",
        formatDoc: "Doc simple",
        formatOriginal: "Format original",
        modelDedicated: "Modele dedie pour cette paire de langues.",
        modelFallback: "Pas de modele dedie pour cette paire, utilise le modele multilingue.",
        modelAutoDetect: "La langue source sera detectee automatiquement.",
        autoDetect: "Detection automatique",
        ownJobDone: "Ton job est termine :"
      }
    };
    const AUTO_LANGUAGE = {code: "auto", name: "auto"};
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
      arb: "Arabic",
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
      lao: "Lao",
      lit: "Lithuanian",
      lvs: "Latvian",
      mal: "Malayalam",
      mar: "Marathi",
      mkd: "Macedonian",
      mlt: "Maltese",
      mya: "Burmese",
      nld: "Dutch",
      npi: "Nepali",
      ory: "Odia",
      pan: "Punjabi",
      pes: "Western Persian",
      pol: "Polish",
      por: "Portuguese",
      rus: "Russian",
      sin: "Sinhala",
      slk: "Slovak",
      slv: "Slovenian",
      spa: "Spanish",
      sqi: "Albanian",
      srp: "Serbian",
      swe: "Swedish",
      tam: "Tamil",
      tel: "Telugu",
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
      hau: "NG", heb: "IL", hin: "IN", hun: "HU", hye: "AM",
      ibo: "NG", ind: "ID", isl: "IS", ita: "IT", jav: "ID", jpn: "JP",
      kan: "IN", kat: "GE", kaz: "KZ", khm: "KH", kir: "KG",
      lao: "LA", lit: "LT", lvs: "LV", mal: "IN", mar: "IN", mkd: "MK",
      mlt: "MT", mya: "MM", nld: "NL", npi: "NP", ory: "IN",
      pan: "IN", pes: "IR", pol: "PL", por: "PT", rus: "RU",
      sin: "LK", slk: "SK", slv: "SI", spa: "ES", sqi: "AL", srp: "RS", swe: "SE",
      tam: "IN", tel: "IN", tur: "TR", ukr: "UA", urd: "PK",
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
      setText('[data-input-tab="textarea"] .tab-label', "textField");
      setText('[data-input-tab="text"] .tab-label', "text");
      setText('[data-input-tab="markdown"] .tab-label', "markdown");
      setText('[data-input-tab="office"] .tab-label', "officeDoc");
      setText('[data-input-tab="pptx"] .tab-label', "pptxFile");
      setText('[data-input-tab="csv"] .tab-label', "csvFile");
      setText('[data-input-tab="website"] .tab-label', "website");
      setText('[data-input-tab="pdf"] .tab-label', "pdf");
      setText("#websiteUrlLabel", "website");
      setText("#websiteHint", "websiteHint");
      setText('label[for="text"]', "textField");
      setText('label[for="pdf"]', "pdf");
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
      updateCounter();
      if (languageData) {
        renderSelect("source", document.getElementById("source").value);
        renderSelect("target", document.getElementById("target").value);
      }
      loadQueue().catch(() => {});
      loadHistory().catch(() => {});
      renderOwnJobBanner();
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
      // Auto-detect is not a language and never belongs in the recents: the source menu already
      // pins it above the favourites, so storing it there listed it a second time.
      if (code === AUTO_LANGUAGE.code) return;
      const recent = [code, ...getRecentLanguages().filter((item) => item !== code)].slice(0, FAVORITE_LANGUAGE_COUNT * 2);
      localStorage.setItem(recentLanguagesKey(), JSON.stringify(recent));
    }

    // Linguinator has no user accounts (Basic Auth, when enabled, shares one credential pair
    // and /jobs is one shared queue with no owner field), so "my jobs" can only mean "jobs this
    // browser started". Tracked here via localStorage; it does not follow you across devices.
    function getOwnJobIds() {
      try {
        return new Set(JSON.parse(localStorage.getItem("linguinator_own_jobs") || "[]"));
      } catch {
        return new Set();
      }
    }

    function rememberOwnJob(jobId) {
      const ids = getOwnJobIds();
      ids.add(jobId);
      localStorage.setItem("linguinator_own_jobs", JSON.stringify([...ids]));
    }

    function getOwnHistoryIds() {
      try {
        return new Set(JSON.parse(localStorage.getItem("linguinator_own_history") || "[]"));
      } catch {
        return new Set();
      }
    }

    function rememberOwnHistory(historyId) {
      const ids = getOwnHistoryIds();
      ids.add(historyId);
      localStorage.setItem("linguinator_own_history", JSON.stringify([...ids]));
    }

    function getFavoriteLanguages() {
      // Filtered on read as well, so browsers that already stored it from an earlier version
      // do not keep showing the duplicate.
      const favorites = getRecentLanguages()
        .filter((code) => code !== AUTO_LANGUAGE.code)
        .slice(0, FAVORITE_LANGUAGE_COUNT);
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
      if (languageCode === AUTO_LANGUAGE.code) return "🔍 " + t("autoDetect");
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
      website: {
        panel: "websitePanel",
        accept: "",
        labelKey: "website",
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
      if (code === AUTO_LANGUAGE.code) return AUTO_LANGUAGE;
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
      if (source === AUTO_LANGUAGE.code) {
        hint.textContent = t("modelAutoDetect");
        hint.classList.remove("hint-dedicated");
        hint.classList.add("hint-fallback");
        return;
      }
      const dedicated = pairs.some((pair) => pair[0] === source && pair[1] === target);
      hint.textContent = t(dedicated ? "modelDedicated" : "modelFallback");
      hint.classList.toggle("hint-dedicated", dedicated);
      hint.classList.toggle("hint-fallback", !dedicated);
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
      const favorites = favoriteCodes.map(languageByCode).filter(Boolean).filter((language) => language.code !== AUTO_LANGUAGE.code);
      select.innerHTML = "";
      menu.innerHTML = "";

      if (id === "source") {
        addOption(select, AUTO_LANGUAGE);
      }
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
      // Auto-detect sits below the favourites in a group of its own: it is a setting, not a
      // language, and listed among them it both read as one and took one of the four slots.
      if (id === "source") {
        const autoDivider = document.createElement("div");
        autoDivider.className = "language-divider";
        menu.appendChild(autoDivider);
        addLanguageGroup(menu, t("detection"));
        addLanguageMenuOption(menu, id, AUTO_LANGUAGE, selectedValue);
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
    }

    document.querySelectorAll("[data-input-tab]").forEach((button) => {
      button.addEventListener("click", () => setInputTab(button.dataset.inputTab));
    });

    document.getElementById("inputTabSelect").addEventListener("change", (event) => {
      setInputTab(event.target.value);
    });

    function setResult(text) {
      fullResultText = text;
    }

    function clearCurrentWork() {
      document.getElementById("text").value = "";
      document.getElementById("textFile").value = "";
      document.getElementById("pdf").value = "";
      document.getElementById("pageRange").value = "";
      document.getElementById("sheetName").value = "";
      document.getElementById("csvColumns").value = "";
      currentSourceFormat = (inputTabs[currentInputTab] || inputTabs.textarea).sourceFormat;
      currentOriginalExtension = currentSourceFormat;
      updateCounter();
      setResult("");
      lastCompletedJob = null;
      renderOwnJobBanner();
      clearProgress();
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

    function clearOwnJobOnNewFile() {
      lastCompletedJob = null;
      renderOwnJobBanner();
    }
    document.getElementById("textFile").addEventListener("change", clearOwnJobOnNewFile);
    document.getElementById("pdf").addEventListener("change", clearOwnJobOnNewFile);

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
      historyTimezone = data.timezone || "UTC";
      timeFormat = data.time_format || "auto";
      updateCounter();
      const versionEl = document.getElementById("appVersion");
      if (versionEl && data.version) versionEl.textContent = " v" + data.version;
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

    function hour12Option() {
      if (timeFormat === "12h") return true;
      if (timeFormat === "24h") return false;
      return undefined;
    }

    function formatJobTime(timestamp) {
      if (!timestamp) return "";
      return new Date(timestamp * 1000).toLocaleString(undefined, {hour12: hour12Option()});
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

    function renderOwnJobBanner() {
      const banner = document.getElementById("ownJobBanner");
      const primaryActions = document.getElementById("primaryActions");
      banner.innerHTML = "";
      const historyItem = lastCompletedJob && lastCompletedJob.history_id
        ? historyItems.find((item) => item.id === lastCompletedJob.history_id)
        : null;
      if (!historyItem) {
        banner.classList.add("hidden");
        primaryActions.classList.remove("hidden");
        return;
      }
      banner.classList.remove("hidden");
      primaryActions.classList.add("hidden");
      const label = document.createElement("p");
      label.className = "subtle";
      label.textContent = t("ownJobDone") + " " + (lastCompletedJob.label || lastCompletedJob.kind) + " (" + t("history") + ")";
      const row = buildHistoryRow(historyItem);
      row.querySelector(".history-download").addEventListener("click", () => {
        setTimeout(() => {
          lastCompletedJob = null;
          renderOwnJobBanner();
        }, 0);
      });
      banner.appendChild(label);
      banner.appendChild(row);
    }

    async function loadQueue() {
      const response = await fetch("jobs");
      if (!response.ok) return;
      const data = await response.json();
      let hasNewlyCompleted = false;
      const ownJobIds = getOwnJobIds();
      for (const job of data.items) {
        if (job.status === "complete" && !seenCompletedJobIds.has(job.id)) {
          seenCompletedJobIds.add(job.id);
          hasNewlyCompleted = true;
          // Catches a job this browser started that finished after a reload, when pollJob is no
          // longer actively watching it.
          if (job.history_id && ownJobIds.has(job.id)) rememberOwnHistory(job.history_id);
        }
      }
      if (hasNewlyCompleted) loadHistory();
      const queue = document.getElementById("queue");
      queue.innerHTML = "";
      const visibleItems = data.items.filter((job) => !["complete", "failed", "cancelled"].includes(job.status));
      updateQueueControlButtons(visibleItems);
      if (!visibleItems.length) {
        queue.textContent = t("noQueuedJobs");
        queue.classList.add("queue-empty-message");
        return;
      }
      queue.classList.remove("queue-empty-message");
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
        const languages = [job.source, job.target].filter(Boolean).map(formatLanguageLabel).join(" -> ");
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
          if (job.history_id && getOwnJobIds().has(job.id)) rememberOwnHistory(job.history_id);
          renderOwnJobBanner();
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
      renderOwnJobBanner();
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
      rememberOwnJob(data.job_id);
      activeJobId = data.job_id;
      const token = ++pollToken;
      loadQueue();
      await pollJob(data.job_id, token);
    }

    async function startCurrentJob() {
      if (currentInputTab === "website") {
        await postUrlJob();
        return;
      }
      if (currentInputTab === "pdf") {
        await postPdfJob();
        return;
      }
      await startTextJob();
    }

    document.getElementById("translate").addEventListener("click", startCurrentJob);

    async function postUrlJob() {
      const url = document.getElementById("websiteUrl").value.trim();
      if (!/^https?:\/\/\S+$/i.test(url)) {
        showProgress("failed", 0, t("websiteInvalid"));
        return;
      }
      const source = document.getElementById("source").value;
      const target = document.getElementById("target").value;
      saveRecent("source", source);
      saveRecent("target", target);
      renderSelect("source", source);
      renderSelect("target", target);
      currentSourceFormat = "md";
      const form = new FormData();
      form.append("url", url);
      form.append("source", source);
      form.append("target", target);
      setResult("");
      lastCompletedJob = null;
      renderOwnJobBanner();
      showProgress("extracting", 0, t("fetchingPage"));
      const response = await fetch("jobs/translate-url", {method: "POST", body: form});
      if (!response.ok) {
        const text = await response.text();
        showProgress("failed", 0, errorTextFromResponse(text));
        setResult("");
        return;
      }
      const data = await response.json();
      ensureAudioContext();
      rememberOwnJob(data.job_id);
      activeJobId = data.job_id;
      const token = ++pollToken;
      loadQueue();
      await pollJob(data.job_id, token);
    }

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
      const form = new FormData();
      form.append("file", file);
      form.append("source", source);
      form.append("target", target);
      form.append("page_range", document.getElementById("pageRange").value);
      setResult("");
      lastCompletedJob = null;
      renderOwnJobBanner();
      showProgress("extracting", 0, t("uploadingPdf"));
      // Always the layout-preserving job: it falls back to the plain/OCR pipeline server-side
      // for scanned PDFs. Plain output stays available afterwards via the TXT/Markdown/PDF/Doc
      // download buttons, which work from the stored result text regardless of which pipeline ran.
      const response = await fetch("jobs/translate-pdf-layout", {method: "POST", body: form});
      if (!response.ok) {
        const text = await response.text();
        showProgress("failed", 0, errorTextFromResponse(text));
        setResult("");
        return;
      }
      const data = await response.json();
      ensureAudioContext();
      rememberOwnJob(data.job_id);
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

    function formatHistoryDate(isoString) {
      try {
        return new Intl.DateTimeFormat(currentUiLanguage, {year: "numeric", month: "2-digit", day: "2-digit", timeZone: historyTimezone}).format(new Date(isoString));
      } catch {
        return (isoString || "").slice(0, 10);
      }
    }

    function formatHistoryTime(isoString) {
      try {
        return new Intl.DateTimeFormat(currentUiLanguage, {hour: "2-digit", minute: "2-digit", timeZone: historyTimezone, hour12: hour12Option()}).format(new Date(isoString));
      } catch {
        return "";
      }
    }

    function historyDisplayName(item) {
      const date = formatHistoryDate(item.created_at);
      const time = formatHistoryTime(item.created_at);
      return item.original_name + " — " + date + (time ? " " + time : "");
    }

    function formatBytes(bytes) {
      if (!bytes) return "0 B";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
      return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    async function loadHistory() {
      const response = await fetch("history");
      const data = await response.json();
      historyItems = data.items || [];
      renderHistory();
      renderOwnJobBanner();
    }

    function filteredHistoryItems() {
      const needle = historyFilterText.trim().toLowerCase();
      if (!needle) return historyItems;
      return historyItems.filter((item) => {
        const haystack = [item.original_name, item.source, item.target].join(" ").toLowerCase();
        return haystack.includes(needle);
      });
    }

    function buildHistoryRow(item) {
      const row = document.createElement("div");
      row.className = "history-row";
      if (lastCompletedJob && lastCompletedJob.history_id === item.id) {
        row.classList.add("history-row-own-current");
      } else if (getOwnHistoryIds().has(item.id)) {
        row.classList.add("history-row-own");
      }
      const main = document.createElement("div");
      main.className = "history-main";
      const link = document.createElement("a");
      link.textContent = historyDisplayName(item);
      const meta = document.createElement("div");
      meta.className = "history-meta";
      // Names, not codes: "deu_Latn -> eng_Latn" is unreadable at a glance in a long list.
      meta.textContent = formatLanguageLabel(item.source) + " -> " + formatLanguageLabel(item.target)
        + " | " + formatBytes(item.size_bytes);
      const format = document.createElement("select");
      format.className = "history-format";
      format.title = "Select the history download format.";
      const hasOriginal = item.has_source_file && item.source_extension;
      const historyFormats = ["md", "txt", "pdf", "doc"];
      if (hasOriginal) {
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
      download.textContent = t("download");
      download.title = "Download this history item.";
      const syncDownloadHref = () => {
        const href = format.value === "md"
          ? "history/" + item.id
          : "history/" + item.id + "/export?format=" + encodeURIComponent(format.value);
        // Empty, not the stored name: the server builds "<document>_<date>_<time>_<language>"
        // in the Content-Disposition header, and any name set here would override it.
        download.href = href;
        download.download = "";
        link.href = href;
        link.download = "";
      };
      if (hasOriginal) format.value = "original";
      syncDownloadHref();
      format.addEventListener("change", syncDownloadHref);
      main.appendChild(link);
      main.appendChild(meta);
      const actions = document.createElement("div");
      actions.className = "history-actions";
      actions.appendChild(format);
      actions.appendChild(download);
      row.appendChild(main);
      row.appendChild(actions);
      return row;
    }

    function renderHistory() {
      const history = document.getElementById("history");
      const pagination = document.getElementById("historyPagination");
      const pageInfo = document.getElementById("historyPageInfo");
      const prevPage = document.getElementById("historyPrevPage");
      const nextPage = document.getElementById("historyNextPage");
      history.innerHTML = "";
      const filtered = filteredHistoryItems();
      if (!historyItems.length) {
        history.textContent = t("noHistory");
        pagination.classList.add("invisible");
        return;
      }
      if (!filtered.length) {
        history.textContent = t("noHistoryMatch");
        pagination.classList.add("invisible");
        return;
      }
      const totalPages = Math.max(1, Math.ceil(filtered.length / HISTORY_PAGE_SIZE));
      historyPage = Math.min(historyPage, totalPages - 1);
      const visibleItems = filtered.slice(historyPage * HISTORY_PAGE_SIZE, historyPage * HISTORY_PAGE_SIZE + HISTORY_PAGE_SIZE);
      for (const item of visibleItems) {
        history.appendChild(buildHistoryRow(item));
      }
      pagination.classList.toggle("invisible", totalPages <= 1);
      pageInfo.textContent = t("pageInfo", {page: historyPage + 1, total: totalPages});
      prevPage.disabled = historyPage <= 0;
      nextPage.disabled = historyPage >= totalPages - 1;
    }

    document.getElementById("historyFilter").addEventListener("input", (event) => {
      historyFilterText = event.target.value || "";
      historyPage = 0;
      renderHistory();
    });

    document.getElementById("historyPrevPage").addEventListener("click", () => {
      historyPage = Math.max(0, historyPage - 1);
      renderHistory();
    });

    document.getElementById("historyNextPage").addEventListener("click", () => {
      historyPage += 1;
      renderHistory();
    });

    function setPanelCollapsed(bodyId, iconId, storageKey, collapsed) {
      const body = document.getElementById(bodyId);
      const icon = document.getElementById(iconId);
      body.classList.toggle("hidden", collapsed);
      icon.classList.toggle("collapsed", collapsed);
      localStorage.setItem(storageKey, collapsed ? "1" : "0");
    }

    function setupPanelCollapse(toggleId, bodyId, iconId, storageKey, collapsedByDefault = false) {
      document.getElementById(toggleId).addEventListener("click", () => {
        const body = document.getElementById(bodyId);
        setPanelCollapsed(bodyId, iconId, storageKey, !body.classList.contains("hidden"));
      });
      // Only an explicit "0" counts as "the user opened it": an absent key is a first visit,
      // which for the queue means collapsed.
      const stored = localStorage.getItem(storageKey);
      const collapsed = stored === null ? collapsedByDefault : stored === "1";
      setPanelCollapsed(bodyId, iconId, storageKey, collapsed);
    }

    setupPanelCollapse("historyCollapse", "historyBody", "historyCollapseIcon", "linguinator_history_collapsed");
    setupPanelCollapse("queueCollapse", "queueBody", "queueCollapseIcon", "linguinator_queue_collapsed", true);

    function setTheme(theme) {
      document.documentElement.dataset.theme = theme;
      document.getElementById("themeToggle").setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      localStorage.setItem("linguinator_theme", theme);
    }

    document.getElementById("themeToggle").addEventListener("click", () => {
      setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
    });

    setTheme(localStorage.getItem("linguinator_theme") || "dark");

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
