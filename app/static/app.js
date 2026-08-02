    let languageData = null;
    let maxChars = 0;
    let activeJobId = null;
    let currentInputTab = "textarea";
    let currentSourceFormat = "txt";
    let currentOriginalExtension = "txt";
    let fullResultText = "";
    let currentUiLanguage = localStorage.getItem("lingumachina_ui_language") || "en";
    let ocrEnabled = false;
    let ocrLanguage = "";
    const baseTitle = document.title || "Lingumachina";
    const PREVIEW_MAX_CHARS = 12000;
    const EXCERPT_MAX_CHARS = 4000;
    const originalExportExtensions = ["docx", "odt", "pptx", "csv", "xlsx", "html", "htm", "srt", "vtt", "json", "yaml", "yml", "po", "xlf", "xliff"];
    const outputFormatKeys = {txt: "formatTxt", md: "formatMarkdown", pdf: "formatPdf", original: "formatOriginal"};
    const uiText = {
      en: {
        uiLanguage: "UI Language",
        subtitle: "Local translation workbench for text and document workflows.",
        queue: "Queue",
        source: "Source",
        target: "Target",
        loading: "Loading...",
        textField: "Text Field",
        text: "Text",
        markdown: "Markdown",
        officeDoc: "Office Doc",
        csvFile: "CSV File",
        pdf: "PDF",
        textFile: "Text File",
        markdownFile: "Markdown File",
        loadFile: "Load File",
        overlayPdf: "Overlay PDF",
        coverOldText: "Cover old text",
        translateInput: "Translate Input",
        pause: "Pause",
        resume: "Resume",
        stop: "Stop",
        preview: "Preview",
        downloadFormat: "Download Format",
        download: "Download",
        delete: "Delete",
        history: "History",
        noQueuedJobs: "No queued jobs.",
        noHistory: "No saved translations yet.",
        queued: "Queued",
        started: "Started",
        chunks: "chunks",
        workerSingular: "worker",
        workerPlural: "workers",
        jobCancelled: "Job cancelled.",
        jobFailed: "Job failed.",
        selectFileFirst: "Select a supported text, document, table, subtitle, or localization file first.",
        selectPdfFirst: "Select a PDF first.",
        originalNeedsFile: "Original format export needs the loaded source file in the input tab.",
        extracting: "Extracting",
        starting: "Starting...",
        startingChunks: "Starting {count} chunks...",
        uploadingPdf: "Uploading PDF...",
        ocrConfigured: "OCR configured: {language}",
        ocrDisabled: "OCR disabled",
        previewExcerptTruncated: "Preview shows only a translated excerpt. The download contains the full export.",
        previewExcerpt: "Preview shows a translated excerpt for this file type. The download contains the full export.",
        previewTruncated: "Preview truncated. The download contains the full export.",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Original Format"
      },
      de: {
        uiLanguage: "UI-Sprache",
        subtitle: "Lokale Uebersetzungsoberflaeche fuer Text- und Dokument-Workflows.",
        queue: "Warteschlange",
        source: "Quelle",
        target: "Ziel",
        loading: "Laedt...",
        textField: "Textfeld",
        text: "Text",
        markdown: "Markdown",
        officeDoc: "Office-Dokument",
        csvFile: "CSV-Datei",
        pdf: "PDF",
        textFile: "Textdatei",
        markdownFile: "Markdown-Datei",
        loadFile: "Datei laden",
        overlayPdf: "PDF ueberlagern",
        coverOldText: "Alten Text abdecken",
        translateInput: "Eingabe uebersetzen",
        pause: "Pause",
        resume: "Fortsetzen",
        stop: "Stoppen",
        preview: "Vorschau",
        downloadFormat: "Download-Format",
        download: "Download",
        delete: "Loeschen",
        history: "History",
        noQueuedJobs: "Keine wartenden Jobs.",
        noHistory: "Noch keine gespeicherten Uebersetzungen.",
        queued: "Eingereiht",
        started: "Gestartet",
        chunks: "Chunks",
        workerSingular: "Worker",
        workerPlural: "Worker",
        jobCancelled: "Job abgebrochen.",
        jobFailed: "Job fehlgeschlagen.",
        selectFileFirst: "Waehle zuerst eine unterstuetzte Text-, Dokument-, Tabellen-, Untertitel- oder Lokalisierungsdatei.",
        selectPdfFirst: "Waehle zuerst eine PDF aus.",
        originalNeedsFile: "Originalformat-Export braucht die geladene Quelldatei im Eingabe-Tab.",
        extracting: "Extrahiere",
        starting: "Starte...",
        startingChunks: "Starte {count} Chunks...",
        uploadingPdf: "Lade PDF hoch...",
        ocrConfigured: "OCR konfiguriert: {language}",
        ocrDisabled: "OCR deaktiviert",
        previewExcerptTruncated: "Die Vorschau zeigt nur einen uebersetzten Auszug. Der Download enthaelt den kompletten Export.",
        previewExcerpt: "Die Vorschau zeigt fuer diesen Dateityp einen uebersetzten Auszug. Der Download enthaelt den kompletten Export.",
        previewTruncated: "Vorschau gekuerzt. Der Download enthaelt den kompletten Export.",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Originalformat"
      },
      es: {
        uiLanguage: "Idioma de UI",
        subtitle: "Banco local de traduccion para flujos de texto y documentos.",
        queue: "Cola",
        source: "Origen",
        target: "Destino",
        loading: "Cargando...",
        textField: "Campo de texto",
        text: "Texto",
        markdown: "Markdown",
        officeDoc: "Documento Office",
        csvFile: "Archivo CSV",
        pdf: "PDF",
        textFile: "Archivo de texto",
        markdownFile: "Archivo Markdown",
        loadFile: "Cargar archivo",
        overlayPdf: "Superponer PDF",
        coverOldText: "Cubrir texto anterior",
        translateInput: "Traducir entrada",
        pause: "Pausar",
        resume: "Continuar",
        stop: "Detener",
        preview: "Vista previa",
        downloadFormat: "Formato de descarga",
        download: "Descargar",
        delete: "Eliminar",
        history: "Historial",
        noQueuedJobs: "No hay trabajos en cola.",
        noHistory: "Aun no hay traducciones guardadas.",
        queued: "En cola",
        started: "Iniciado",
        chunks: "fragmentos",
        workerSingular: "worker",
        workerPlural: "workers",
        jobCancelled: "Trabajo cancelado.",
        jobFailed: "El trabajo fallo.",
        selectFileFirst: "Selecciona primero un archivo compatible de texto, documento, tabla, subtitulos o localizacion.",
        selectPdfFirst: "Selecciona primero un PDF.",
        originalNeedsFile: "La exportacion en formato original necesita el archivo fuente cargado en la pestana de entrada.",
        extracting: "Extrayendo",
        starting: "Iniciando...",
        startingChunks: "Iniciando {count} fragmentos...",
        uploadingPdf: "Subiendo PDF...",
        ocrConfigured: "OCR configurado: {language}",
        ocrDisabled: "OCR desactivado",
        previewExcerptTruncated: "La vista previa muestra solo un extracto traducido. La descarga contiene la exportacion completa.",
        previewExcerpt: "La vista previa muestra un extracto traducido para este tipo de archivo. La descarga contiene la exportacion completa.",
        previewTruncated: "Vista previa recortada. La descarga contiene la exportacion completa.",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Formato original"
      },
      fr: {
        uiLanguage: "Langue UI",
        subtitle: "Atelier local de traduction pour les workflows texte et documents.",
        queue: "File d'attente",
        source: "Source",
        target: "Cible",
        loading: "Chargement...",
        textField: "Champ texte",
        text: "Texte",
        markdown: "Markdown",
        officeDoc: "Document Office",
        csvFile: "Fichier CSV",
        pdf: "PDF",
        textFile: "Fichier texte",
        markdownFile: "Fichier Markdown",
        loadFile: "Charger le fichier",
        overlayPdf: "Superposer PDF",
        coverOldText: "Masquer l'ancien texte",
        translateInput: "Traduire l'entree",
        pause: "Pause",
        resume: "Reprendre",
        stop: "Arreter",
        preview: "Apercu",
        downloadFormat: "Format de telechargement",
        download: "Telecharger",
        delete: "Supprimer",
        history: "Historique",
        noQueuedJobs: "Aucun job en file.",
        noHistory: "Aucune traduction enregistree.",
        queued: "En file",
        started: "Demarre",
        chunks: "segments",
        workerSingular: "worker",
        workerPlural: "workers",
        jobCancelled: "Job annule.",
        jobFailed: "Le job a echoue.",
        selectFileFirst: "Selectionne d'abord un fichier compatible texte, document, tableau, sous-titres ou localisation.",
        selectPdfFirst: "Selectionne d'abord un PDF.",
        originalNeedsFile: "L'export au format original a besoin du fichier source charge dans l'onglet d'entree.",
        extracting: "Extraction",
        starting: "Demarrage...",
        startingChunks: "Demarrage de {count} segments...",
        uploadingPdf: "Televersement du PDF...",
        ocrConfigured: "OCR configure: {language}",
        ocrDisabled: "OCR desactive",
        previewExcerptTruncated: "L'apercu affiche seulement un extrait traduit. Le telechargement contient l'export complet.",
        previewExcerpt: "L'apercu affiche un extrait traduit pour ce type de fichier. Le telechargement contient l'export complet.",
        previewTruncated: "Apercu tronque. Le telechargement contient l'export complet.",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF",
        formatOriginal: "Format original"
      }
    };
    const favoriteLanguages = ["deu_Latn", "eng_Latn", "fra_Latn", "spa_Latn", "ita_Latn"];
    const languageNames = new Intl.DisplayNames(["en"], {type: "language"});
    const scriptNames = {
      Adlm: "Adlam",
      Arab: "Arabic",
      Armn: "Armenian",
      Beng: "Bengali",
      Cans: "Canadian Aboriginal",
      Cyrl: "Cyrillic",
      Deva: "Devanagari",
      Ethi: "Ethiopic",
      Geor: "Georgian",
      Grek: "Greek",
      Gujr: "Gujarati",
      Guru: "Gurmukhi",
      Hans: "Simplified Han",
      Hant: "Traditional Han",
      Hebr: "Hebrew",
      Jpan: "Japanese",
      Khmr: "Khmer",
      Knda: "Kannada",
      Kore: "Korean",
      Laoo: "Lao",
      Latn: "Latin",
      Mlym: "Malayalam",
      Mtei: "Meitei",
      Mymr: "Myanmar",
      Orya: "Odia",
      Sinh: "Sinhala",
      Taml: "Tamil",
      Telu: "Telugu",
      Tfng: "Tifinagh",
      Thai: "Thai",
      Tibt: "Tibetan",
      Vaii: "Vai"
    };
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
      setText('[data-input-tab="csv"]', "csvFile");
      setText('[data-input-tab="pdf"]', "pdf");
      setText('label[for="text"]', "textField");
      setText('label[for="pdf"]', "pdf");
      setText('label[for="usePdfOverlay"]', "overlayPdf");
      setText('label[for="coverPdfText"]', "coverOldText");
      setText("#loadTextFile", "loadFile");
      setText("#translate", "translateInput");
      setText("#pauseJob", "pause");
      setText("#resumeJob", "resume");
      setText("#stopJob", "stop");
      setText('label[for="result"]', "preview");
      setText('label[for="outputFormat"]', "downloadFormat");
      setText("#downloadResult", "download");
      setText('label[for="history"]', "history");
      setTitle("#uiLanguage", "uiLanguage");
      refreshInputLabels();
      refreshOutputFormats(document.getElementById("outputFormat").value);
      updateOcrLabel();
      updateCounter();
      loadQueue().catch(() => {});
      loadHistory().catch(() => {});
    }

    function setupUiLanguagePicker() {
      const select = document.getElementById("uiLanguage");
      if (!select) return;
      select.value = currentUiLanguage;
      select.addEventListener("change", () => {
        currentUiLanguage = select.value || "en";
        localStorage.setItem("lingumachina_ui_language", currentUiLanguage);
        applyUiLanguage();
      });
    }

    function recentKey(id) {
      return "lingumachina_recent_" + id;
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
    }

    function addOption(select, language) {
      const option = document.createElement("option");
      option.value = language.code;
      option.textContent = formatLanguageLabel(language.code);
      select.appendChild(option);
    }

    function countryFlag(countryCode) {
      if (!countryCode || countryCode === "UN") return "";
      return countryCode
        .toUpperCase()
        .replace(/./g, (char) => String.fromCodePoint(127397 + char.charCodeAt(0)));
    }

    function titleCase(text) {
      return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
    }

    function languageDisplayName(languageCode) {
      const [languagePart, scriptPart] = languageCode.split("_");
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
      const script = scriptNames[scriptPart] || scriptPart;
      const flag = countryFlag(languageCountries[languagePart]);
      return {name, script, flag};
    }

    function formatLanguageLabel(languageCode) {
      const display = languageDisplayName(languageCode);
      return (display.flag ? display.flag + " " : "") + display.name + " (" + display.script + ")";
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
        accept: ".docx,.odt,.pptx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.oasis.opendocument.text,application/vnd.openxmlformats-officedocument.presentationml.presentation",
        labelKey: "officeDoc",
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

    function setPickerValue(id, code) {
      const select = document.getElementById(id);
      const button = document.getElementById(id + "Button");
      select.value = code;
      button.textContent = formatLanguageLabel(code);
      document.querySelectorAll("#" + id + "Menu .language-option").forEach((option) => {
        option.classList.toggle("active", option.dataset.code === code);
      });
      closeLanguageMenus();
    }

    function closeLanguageMenus() {
      document.querySelectorAll(".language-field.open").forEach((field) => {
        field.classList.remove("open");
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
    }

    function renderSelect(id, selectedValue) {
      const select = document.getElementById(id);
      const menu = document.getElementById(id + "Menu");
      const favorites = favoriteLanguages.map(languageByCode).filter(Boolean);
      select.innerHTML = "";
      menu.innerHTML = "";

      for (const language of languageData.languages) {
        addOption(select, language);
      }

      addLanguageGroup(menu, "Favorites");
      for (const language of favorites) {
        addLanguageMenuOption(menu, id, language, selectedValue);
      }

      const divider = document.createElement("div");
      divider.className = "language-divider";
      menu.appendChild(divider);
      addLanguageGroup(menu, "All languages");
      for (const language of languageData.languages) {
        if (!favoriteLanguages.includes(language.code)) {
          addLanguageMenuOption(menu, id, language, selectedValue);
        }
      }

      setPickerValue(id, selectedValue);
    }

    function setupLanguagePicker(id) {
      const field = document.querySelector('[data-picker="' + id + '"]');
      const button = document.getElementById(id + "Button");
      button.addEventListener("click", () => {
        const wasOpen = field.classList.contains("open");
        closeLanguageMenus();
        field.classList.toggle("open", !wasOpen);
      });
    }

    async function loadLanguages() {
      const response = await fetch("languages");
      languageData = await response.json();
      renderSelect("source", getRecent("source")[0] || languageData.source_default);
      renderSelect("target", getRecent("target")[0] || languageData.target_default);
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
      refreshOutputFormats();
    }

    document.querySelectorAll("[data-input-tab]").forEach((button) => {
      button.addEventListener("click", () => setInputTab(button.dataset.inputTab));
    });

    function syncOverlayControls() {
      const cover = document.getElementById("coverPdfText");
      cover.disabled = !document.getElementById("usePdfOverlay").checked;
      if (cover.disabled) {
        cover.checked = false;
      }
    }

    document.getElementById("usePdfOverlay").addEventListener("change", syncOverlayControls);

    function previewText(text, limit) {
      if (text.length <= limit) {
        return {text, truncated: false};
      }
      return {
        text: text.slice(0, limit).replace(/\s+\S*$/, "").trimEnd(),
        truncated: true
      };
    }

    function previewLimitForOptions(options) {
      if (options && options.excerpt) return EXCERPT_MAX_CHARS;
      return PREVIEW_MAX_CHARS;
    }

    function setResult(text, options = {}) {
      const result = document.getElementById("result");
      const notice = document.getElementById("previewNotice");
      fullResultText = text;
      setPreview(text, options);
      const hasText = Boolean(text.trim());
      document.getElementById("downloadResult").disabled = !hasText;
    }

    function setPreview(text, options = {}) {
      const result = document.getElementById("result");
      const notice = document.getElementById("previewNotice");
      const hasText = Boolean(text.trim());
      const preview = previewText(text, previewLimitForOptions(options));
      result.textContent = preview.text;
      notice.classList.toggle("visible", hasText && (preview.truncated || options.excerpt));
      if (hasText && options.excerpt) {
        notice.textContent = preview.truncated
          ? t("previewExcerptTruncated")
          : t("previewExcerpt");
      } else if (hasText && preview.truncated) {
        notice.textContent = t("previewTruncated");
      } else {
        notice.textContent = "";
      }
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

    function defaultOutputFormat() {
      if (currentInputTab !== "textarea" && canUseOriginalFormat()) return "original";
      if (currentSourceFormat === "pdf") return "pdf";
      if (currentSourceFormat === "md") return "md";
      return "txt";
    }

    function refreshOutputFormats(preferred) {
      const select = document.getElementById("outputFormat");
      const current = preferred || select.value || defaultOutputFormat();
      const formats = ["txt", "md", "pdf"];
      if (canUseOriginalFormat()) {
        formats.push("original");
      }
      select.innerHTML = "";
      for (const format of formats) {
        const option = document.createElement("option");
        option.value = format;
        option.textContent = t(outputFormatKeys[format]);
        select.appendChild(option);
      }
      select.value = formats.includes(current) ? current : defaultOutputFormat();
    }

    function saveBlob(blob, extension) {
      const link = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = "lingumachina-result-" + stamp + "." + extension;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }

    async function downloadOverlayPdf(text, details) {
      const file = document.getElementById("pdf").files[0];
      const form = new FormData();
      form.append("file", file);
      form.append("text", text);
      form.append("cover_original", document.getElementById("coverPdfText").checked ? "true" : "false");
      const response = await fetch("export-pdf-overlay", {method: "POST", body: form});
      if (!response.ok) {
        const error = await response.text();
        setPreview(errorTextFromResponse(error));
        return;
      }
      saveBlob(await response.blob(), details.extension);
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
        setPreview(t("originalNeedsFile"));
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
        setPreview(errorTextFromResponse(error));
        return;
      }
      saveBlob(await response.blob(), details.extension);
    }

    async function downloadResult() {
      const text = fullResultText;
      if (!text.trim()) return;
      const details = outputFormatDetails(document.getElementById("outputFormat").value);
      if (details.originalFile) {
        await downloadOriginalFile(text, details);
        return;
      }
      if (details.pdf) {
        const pdfFile = document.getElementById("pdf").files[0];
        if (currentSourceFormat === "pdf" && document.getElementById("usePdfOverlay").checked && pdfFile) {
          await downloadOverlayPdf(text, details);
          return;
        }
        const response = await fetch("export-pdf", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({text})
        });
        if (!response.ok) {
          const error = await response.text();
          setPreview(errorTextFromResponse(error));
          return;
        }
        saveBlob(await response.blob(), details.extension);
        return;
      }
      saveBlob(new Blob([text], {type: details.contentType + ";charset=utf-8"}), details.extension);
    }

    async function loadTextFile() {
      const file = document.getElementById("textFile").files[0];
      if (!file) {
        setResult(t("selectFileFirst"));
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
        showProgress("queued", 0, t("extracting") + " " + lowerName.split(".").pop().toUpperCase() + "...");
        const response = await fetch(path, {method: "POST", body: form});
        const text = await response.text();
        if (!response.ok) {
          setResult(errorTextFromResponse(text));
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
      refreshOutputFormats(defaultOutputFormat());
      setResult("");
      clearProgress();
    }

    document.getElementById("downloadResult").addEventListener("click", downloadResult);
    document.getElementById("loadTextFile").addEventListener("click", loadTextFile);

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

    function showProgress(status, percent, info) {
      const progress = document.getElementById("progress");
      const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
      const titleStatus = status.charAt(0).toUpperCase() + status.slice(1);
      document.title = safePercent + "% " + titleStatus + " - " + baseTitle;
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
      const ocr = document.getElementById("useOcr");
      ocrEnabled = Boolean(data.ocr_enabled);
      ocrLanguage = data.ocr_language || "";
      ocr.checked = ocrEnabled;
      ocr.disabled = true;
      updateOcrLabel();
      updateCounter();
    }

    function updateOcrLabel() {
      const ocrLabel = document.getElementById("ocrLabel");
      if (!ocrLabel) return;
      ocrLabel.textContent = ocrEnabled
        ? t("ocrConfigured", {language: ocrLanguage})
        : t("ocrDisabled");
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
        setResult(errorTextFromResponse(text));
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

    async function loadQueue() {
      const response = await fetch("jobs");
      if (!response.ok) return;
      const data = await response.json();
      const queue = document.getElementById("queue");
      const workers = document.getElementById("queueWorkers");
      queue.innerHTML = "";
      workers.textContent = data.workers + " " + t(data.workers === 1 ? "workerSingular" : "workerPlural");
      const visibleItems = data.items.filter((job) => !["complete", "failed", "cancelled"].includes(job.status));
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
        const position = job.position ? "#" + job.position + " " : "";
        title.textContent = position + (job.label || job.kind) + " - " + job.status;
        const meta = document.createElement("div");
        meta.className = "queue-meta";
        const languages = [job.source, job.target].filter(Boolean).join(" -> ");
        const started = job.started_at ? t("started") + " " + formatJobTime(job.started_at) : t("queued") + " " + formatJobTime(job.queued_at);
        meta.textContent = [languages, started].filter(Boolean).join(" | ");
        const progress = document.createElement("div");
        progress.className = "queue-progress";
        progress.textContent = (job.percent || 0) + "% | " + (job.current || 0) + " / " + (job.total || 0) + " " + t("chunks") + " | " + (job.message || "");
        const actions = document.createElement("div");
        actions.className = "queue-actions";
        actions.appendChild(queueActionButton(job, "pause", t("pause"), ["queued", "running"]));
        actions.appendChild(queueActionButton(job, "resume", t("resume"), ["paused"]));
        actions.appendChild(queueActionButton(job, "cancel", t("stop"), ["queued", "running", "paused"]));
        main.appendChild(title);
        main.appendChild(meta);
        main.appendChild(progress);
        row.appendChild(main);
        row.appendChild(actions);
        queue.appendChild(row);
      }
    }

    function updateProgress(job) {
      const current = job.current || 0;
      const total = job.total || 0;
      const percent = job.percent || 0;
      const eta = job.status === "running" ? "ETA " + formatEta(job.eta_seconds) : "";
      showProgress(
        job.status,
        percent,
        percent + "% | " + current + " / " + total + " chunks | " + eta + " " + (job.message || "")
      );
      document.getElementById("pauseJob").disabled = !["queued", "running"].includes(job.status);
      document.getElementById("resumeJob").disabled = job.status !== "paused";
      document.getElementById("stopJob").disabled = !["queued", "running", "paused"].includes(job.status);
    }

    async function pollJob(jobId) {
      while (true) {
        const response = await fetch("jobs/" + jobId);
        if (!response.ok) {
          const text = await response.text();
          setResult(errorTextFromResponse(text));
          activeJobId = null;
          return;
        }
        const job = await response.json();
        updateProgress(job);
        if (job.status === "complete") {
          if (job.kind === "translate-pdf") {
            document.getElementById("outputFormat").value = "pdf";
          }
          setResult(job.result || "", {
            excerpt: job.kind === "translate-pdf" || currentInputTab === "office"
          });
          loadHistory();
          loadQueue();
          activeJobId = null;
          return;
        }
        if (job.status === "cancelled") {
          setResult(t("jobCancelled"));
          loadQueue();
          activeJobId = null;
          return;
        }
        if (job.status === "failed") {
          setResult(job.error || t("jobFailed"));
          loadQueue();
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
        setResult(errorTextFromResponse(text));
        clearProgress();
        return;
      }
      const data = await response.json();
      activeJobId = data.job_id;
      loadQueue();
      await pollJob(data.job_id);
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
        setResult(t("selectPdfFirst"));
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
      showProgress("queued", 0, t("uploadingPdf"));
      const response = await fetch("jobs/translate-pdf", {method: "POST", body: form});
      if (!response.ok) {
        const text = await response.text();
        setResult(errorTextFromResponse(text));
        clearProgress();
        return;
      }
      const data = await response.json();
      activeJobId = data.job_id;
      loadQueue();
      await pollJob(data.job_id);
    }

    async function controlActiveJob(action) {
      if (!activeJobId) return;
      const job = await controlJob(activeJobId, action);
      if (job) updateProgress(job);
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
        setResult(errorTextFromResponse(text));
        return;
      }
      await loadHistory();
    }

    async function loadHistory() {
      const response = await fetch("history");
      const data = await response.json();
      const history = document.getElementById("history");
      history.innerHTML = "";
      if (!data.items.length) {
        history.textContent = t("noHistory");
        return;
      }
      for (const item of data.items) {
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
    }

    document.getElementById("pauseJob").addEventListener("click", () => controlActiveJob("pause"));
    document.getElementById("resumeJob").addEventListener("click", () => controlActiveJob("resume"));
    document.getElementById("stopJob").addEventListener("click", () => controlActiveJob("cancel"));

    setupUiLanguagePicker();
    loadLanguages().catch((error) => {
      setResult(error.toString());
    });
    loadHealth().catch((error) => {
      setResult(error.toString());
    });
    loadHistory().catch((error) => {
      document.getElementById("history").textContent = error.toString();
    });
    loadQueue().catch(() => {});
    setInterval(() => {
      loadQueue().catch(() => {});
    }, 3000);
    setInputTab("textarea");
    applyUiLanguage();
    syncOverlayControls();
