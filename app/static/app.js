let languageData = null;
    let maxChars = 0;
    let activeJobId = null;
    let lastCompletedJob = null;
    let currentInputTab = "textarea";
    let currentSourceFormat = "txt";
    let currentOriginalExtension = "txt";
    let fullResultText = "";
    // Overwritten from /languages on load unless this browser has been switched by hand, in
    // which case its own choice wins over the installation's default.
    let currentUiLanguage = localStorage.getItem("linguinator_ui_language") || "en";
    let historyItems = [];
    let queueItems = [];
    let hadActiveJobs = false;
    let historyPage = 0;
    let historyFilterText = "";
    let historyTimezone = "UTC";
    let timeFormat = "24h";
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
        textResult: "Result",
        text: "TXT File",
        markdown: "Markdown",
        officeDoc: "DOC File",
        pptxFile: "PPTX File",
        csvFile: "CSV File",
        pdf: "PDF",
        textFile: "TXT File",
        markdownFile: "Markdown File",
        translateInput: "Translate",
        pause: "Pause",
        resume: "Resume",
        stop: "Stop",
        cancel: "Cancel",
        historySelect: "Select",
        historySelectAll: "Select all",
        historyDeleteCount: "Delete ({count})",
        history: "History",
        noHistory: "No saved translations yet.",
        noHistoryMatch: "No history entries match this filter.",
        pageInfo: "Page {page} / {total}",
        queued: "Queued",
        statusRunning: "running",
        statusPaused: "paused",
        statusComplete: "complete",
        statusFailed: "failed",
        statusCancelled: "cancelled",
        queuePosition: "Queue position #{position}",
        watchJob: "Click to follow this job in the tab title.",
        unwatchJob: "Click to stop following it; the tab title goes back to plain Linguinator.",
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
        download: "Download",
        downloadItem: "Download this history item.",
        historyFormat: "Select the history download format.",
        selectLanguage: "Select {language}.",
        pageRangeHint: "Pages, e.g. 1-3,5",
        sheetNameLabel: "XLSX Sheet",
        sheetNameHint: "Optional, first sheet if empty",
        csvColumnsLabel: "Columns to translate",
        csvColumnsHint: "Optional, e.g. title,description or A,B",
        historyFilterHint: "Filter history...",
        tipReload: "Reload Linguinator.",
        tipUiLanguage: "Select the interface language.",
        tipTheme: "Toggle light/dark mode.",
        tipTabTextarea: "Type text directly into the input field.",
        tipTabText: "Load and translate a TXT file, and also HTML, SRT, VTT, JSON, YAML, PO or XLIFF.",
        tipTabMarkdown: "Load and translate a Markdown file.",
        tipTabOffice: "Load and translate a DOCX or ODT file as text.",
        tipTabPptx: "Load and translate a PowerPoint (PPTX) file as text.",
        tipTabCsv: "Load and translate selected CSV or XLSX columns.",
        tipTabPdf: "Extract and translate a PDF page by page.",
        tipInputType: "Select the input type.",
        tipSource: "Select the source language.",
        tipTarget: "Select the target language.",
        tipTextarea: "Enter text or load it from a file.",
        tipCounter: "Characters used, character limit, and estimated translation chunks.",
        tipTextResult: "The translation, once the job completes.",
        tipFilePick: "Select the file to load.",
        tipSheetName: "Which sheet to use in an XLSX file. Leave empty for the first sheet. Not used for CSV files, and not used if the columns field below lists its own sheets.",
        tipCsvColumns: "Column names or letters to translate. Leave empty to translate every cell (numbers, IP and MAC addresses are skipped automatically). For several XLSX sheets at once, one \"Sheet: columns\" line per sheet, overriding the sheet field above.",
        tipPdfPick: "Select the PDF to translate.",
        tipPageRange: "Optional page range, e.g. 1-3,5. Leave empty to translate all pages.",
        tipTranslate: "Start a new translation for the current input.",
        tipPause: "Pause the entire queue.",
        tipResume: "Resume the entire queue.",
        tipStop: "Cancel the entire queue.",
        tipHistoryReset: "Select history entries to delete.",
        tipHistoryToggle: "Show or hide the history list.",
        tipHistoryFilter: "Filter history by filename or languages.",
        tipPrevPage: "Previous page.",
        tipNextPage: "Next page.",
        tipRepo: "Open the Linguinator repository.",
        modelDedicated: "Dedicated model for this language pair.",
        modelFallback: "No dedicated model for this pair, using the multilingual fallback.",
        modelAutoDetect: "Detected automatically. Scans take longer, and a scan mixing two scripts needs the language set.",
        autoDetect: "Auto-detect",
      },
      de: {
        uiLanguage: "UI-Sprache",
        subtitle: "Lokale Übersetzungsoberfläche für Text- und Dokument-Workflows.",
        queue: "Warteschlange",
        source: "Quelle",
        target: "Ziel",
        searchLanguage: "Sprache suchen...",
        detection: "Erkennung",
        favorites: "Favoriten",
        allLanguages: "Alle Sprachen",
        loading: "Lädt...",
        textField: "Textfeld",
        textResult: "Ergebnis",
        text: "TXT-Datei",
        markdown: "Markdown",
        officeDoc: "Office-Dokument",
        pptxFile: "PPTX-Datei",
        csvFile: "CSV-Datei",
        pdf: "PDF",
        textFile: "TXT-Datei",
        markdownFile: "Markdown-Datei",
        translateInput: "Übersetzen",
        pause: "Pause",
        resume: "Fortsetzen",
        stop: "Stoppen",
        cancel: "Abbrechen",
        historySelect: "Auswählen",
        historySelectAll: "Alle auswählen",
        historyDeleteCount: "Löschen ({count})",
        history: "Verlauf",
        noHistory: "Noch keine gespeicherten Übersetzungen.",
        noHistoryMatch: "Kein Verlaufseintrag passt zu diesem Filter.",
        pageInfo: "Seite {page} / {total}",
        queued: "Eingereiht",
        statusRunning: "läuft",
        statusPaused: "pausiert",
        statusComplete: "fertig",
        statusFailed: "fehlgeschlagen",
        statusCancelled: "abgebrochen",
        queuePosition: "Warteschlangenposition #{position}",
        watchJob: "Klicken, um diesen Job im Tab-Titel zu verfolgen.",
        unwatchJob: "Klicken, um die Verfolgung zu beenden; der Tab-Titel zeigt wieder nur Linguinator.",
        started: "Gestartet",
        chunks: "Chunks",
        jobFailed: "Job fehlgeschlagen.",
        selectFileFirst: "Wähle zuerst eine unterstützte Text-, Dokument-, Tabellen-, Untertitel- oder Lokalisierungsdatei.",
        selectPdfFirst: "Wähle zuerst eine PDF aus.",
        extracting: "Extrahiere",
        starting: "Starte...",
        startingChunks: "Starte {count} Chunks...",
        uploadingPdf: "Lade PDF hoch...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "Nur Text-PDF",
        formatDoc: "Nur Text-Doc",
        formatOriginal: "Originalformat",
        download: "Herunterladen",
        downloadItem: "Diesen Verlaufseintrag herunterladen.",
        historyFormat: "Format für den Verlauf-Download wählen.",
        selectLanguage: "{language} auswählen.",
        pageRangeHint: "Seiten, z. B. 1-3,5",
        sheetNameLabel: "XLSX-Blatt",
        sheetNameHint: "Optional, ohne Angabe erstes Blatt",
        csvColumnsLabel: "Zu übersetzende Spalten",
        csvColumnsHint: "Optional, z. B. titel,beschreibung oder A,B",
        historyFilterHint: "Verlauf filtern...",
        tipReload: "Linguinator neu laden.",
        tipUiLanguage: "Sprache der Oberfläche wählen.",
        tipTheme: "Zwischen hell und dunkel wechseln.",
        tipTabTextarea: "Text direkt ins Eingabefeld tippen.",
        tipTabText: "TXT-Datei laden und übersetzen, ebenso HTML, SRT, VTT, JSON, YAML, PO oder XLIFF.",
        tipTabMarkdown: "Markdown-Datei laden und übersetzen.",
        tipTabOffice: "DOCX- oder ODT-Datei als Text laden und übersetzen.",
        tipTabPptx: "PowerPoint-Datei (PPTX) als Text laden und übersetzen.",
        tipTabCsv: "Ausgewählte CSV- oder XLSX-Spalten laden und übersetzen.",
        tipTabPdf: "PDF seitenweise auslesen und übersetzen.",
        tipInputType: "Eingabeart wählen.",
        tipSource: "Quellsprache wählen.",
        tipTarget: "Zielsprache wählen.",
        tipTextarea: "Text eingeben oder aus einer Datei laden.",
        tipCounter: "Verbrauchte Zeichen, Zeichengrenze und geschätzte Chunks.",
        tipTextResult: "Die Übersetzung, sobald der Job abgeschlossen ist.",
        tipFilePick: "Zu ladende Datei wählen.",
        tipSheetName: "Welches Blatt einer XLSX-Datei verwendet wird. Leer lassen für das erste Blatt. Wird bei CSV-Dateien nicht verwendet, und nicht, wenn das Spalten-Feld unten eigene Blätter angibt.",
        tipCsvColumns: "Zu übersetzende Spaltennamen oder -buchstaben. Leer lassen, um jede Zelle zu übersetzen (Zahlen, IP- und MAC-Adressen werden automatisch übersprungen). Für mehrere XLSX-Blätter auf einmal: pro Blatt eine Zeile \"Blatt: Spalten\", das ersetzt das Blatt-Feld oben.",
        tipPdfPick: "Zu uebersetzende PDF wählen.",
        tipPageRange: "Optionaler Seitenbereich, z. B. 1-3,5. Leer übersetzt alle Seiten.",
        tipTranslate: "Neue Übersetzung für die aktuelle Eingabe starten.",
        tipPause: "Gesamte Warteschlange pausieren.",
        tipResume: "Gesamte Warteschlange fortsetzen.",
        tipStop: "Gesamte Warteschlange abbrechen.",
        tipHistoryReset: "Verlaufseinträge zum Löschen auswählen.",
        tipHistoryToggle: "Verlauf ein- oder ausklappen.",
        tipHistoryFilter: "Verlauf nach Dateiname oder Sprachen filtern.",
        tipPrevPage: "Vorherige Seite.",
        tipNextPage: "Nächste Seite.",
        tipRepo: "Linguinator-Repository öffnen.",
        modelDedicated: "Eigenes Modell für dieses Sprachpaar.",
        modelFallback: "Kein eigenes Modell für dieses Paar, nutzt den mehrsprachigen Fallback.",
        modelAutoDetect: "Wird automatisch erkannt. Scans dauern länger, bei zwei Schriften die Sprache selbst setzen.",
        autoDetect: "Automatisch erkennen",
      },
      es: {
        uiLanguage: "Idioma de UI",
        subtitle: "Banco local de traducción para flujos de texto y documentos.",
        queue: "Cola",
        source: "Origen",
        target: "Destino",
        searchLanguage: "Buscar idioma...",
        detection: "Detección",
        favorites: "Favoritos",
        allLanguages: "Todos los idiomas",
        loading: "Cargando...",
        textField: "Campo de texto",
        textResult: "Resultado",
        text: "Archivo TXT",
        markdown: "Markdown",
        officeDoc: "Documento Office",
        pptxFile: "Archivo PPTX",
        csvFile: "Archivo CSV",
        pdf: "PDF",
        textFile: "Archivo TXT",
        markdownFile: "Archivo Markdown",
        translateInput: "Traducir",
        pause: "Pausar",
        resume: "Continuar",
        stop: "Detener",
        cancel: "Cancelar",
        historySelect: "Seleccionar",
        historySelectAll: "Seleccionar todo",
        historyDeleteCount: "Eliminar ({count})",
        history: "Historial",
        noHistory: "Aún no hay traducciones guardadas.",
        noHistoryMatch: "Ningún elemento del historial coincide con este filtro.",
        pageInfo: "Página {page} / {total}",
        queued: "En cola",
        statusRunning: "en curso",
        statusPaused: "en pausa",
        statusComplete: "completo",
        statusFailed: "fallido",
        statusCancelled: "cancelado",
        queuePosition: "Posición en cola #{position}",
        watchJob: "Haz clic para seguir este trabajo en el título de la pestaña.",
        unwatchJob: "Haz clic para dejar de seguirlo; el título vuelve a ser solo Linguinator.",
        started: "Iniciado",
        chunks: "fragmentos",
        jobFailed: "El trabajo falló.",
        selectFileFirst: "Selecciona primero un archivo compatible de texto, documento, tabla, subtítulos o localización.",
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
        download: "Descargar",
        downloadItem: "Descargar este elemento del historial.",
        historyFormat: "Elegir el formato de descarga del historial.",
        selectLanguage: "Seleccionar {language}.",
        pageRangeHint: "Páginas, p. ej. 1-3,5",
        sheetNameLabel: "Hoja XLSX",
        sheetNameHint: "Opcional, primera hoja si está vacío",
        csvColumnsLabel: "Columnas a traducir",
        csvColumnsHint: "Opcional, p. ej. título,descripción o A,B",
        historyFilterHint: "Filtrar historial...",
        tipReload: "Recargar Linguinator.",
        tipUiLanguage: "Elegir el idioma de la interfaz.",
        tipTheme: "Cambiar entre modo claro y oscuro.",
        tipTabTextarea: "Escribir texto directamente en el campo.",
        tipTabText: "Cargar y traducir un archivo TXT, también HTML, SRT, VTT, JSON, YAML, PO o XLIFF.",
        tipTabMarkdown: "Cargar y traducir un archivo Markdown.",
        tipTabOffice: "Cargar y traducir un archivo DOCX u ODT como texto.",
        tipTabPptx: "Cargar y traducir un archivo PowerPoint (PPTX) como texto.",
        tipTabCsv: "Cargar y traducir columnas CSV o XLSX seleccionadas.",
        tipTabPdf: "Extraer y traducir un PDF página por página.",
        tipInputType: "Elegir el tipo de entrada.",
        tipSource: "Elegir el idioma de origen.",
        tipTarget: "Elegir el idioma de destino.",
        tipTextarea: "Escribir el texto o cargarlo de un archivo.",
        tipCounter: "Caracteres usados, límite y bloques estimados.",
        tipTextResult: "La traducción, una vez que el trabajo se complete.",
        tipFilePick: "Elegir el archivo a cargar.",
        tipSheetName: "Qué hoja usar en un archivo XLSX. Vacío para la primera hoja. No se usa en archivos CSV, ni si el campo de columnas de abajo indica sus propias hojas.",
        tipCsvColumns: "Nombres o letras de las columnas a traducir. Déjalo vacío para traducir cada celda (los números y las direcciones IP/MAC se omiten automáticamente). Para varias hojas XLSX a la vez: una línea \"Hoja: columnas\" por hoja, que sustituye al campo de hoja de arriba.",
        tipPdfPick: "Elegir el PDF a traducir.",
        tipPageRange: "Rango de páginas opcional, p. ej. 1-3,5. Vacío traduce todas.",
        tipTranslate: "Iniciar una traducción para la entrada actual.",
        tipPause: "Pausar toda la cola.",
        tipResume: "Continuar toda la cola.",
        tipStop: "Cancelar toda la cola.",
        tipHistoryReset: "Selecciona entradas del historial para eliminar.",
        tipHistoryToggle: "Mostrar u ocultar el historial.",
        tipHistoryFilter: "Filtrar el historial por nombre o idiomas.",
        tipPrevPage: "Página anterior.",
        tipNextPage: "Página siguiente.",
        tipRepo: "Abrir el repositorio de Linguinator.",
        modelDedicated: "Modelo dedicado para este par de idiomas.",
        modelFallback: "Sin modelo dedicado para este par, se usa el alternativo multilingüe.",
        modelAutoDetect: "Se detecta automáticamente. Los escaneos tardan más; si mezclan dos alfabetos, fija el idioma.",
        autoDetect: "Detección automática",
      },
      fr: {
        uiLanguage: "Langue UI",
        subtitle: "Atelier local de traduction pour les workflows texte et documents.",
        queue: "File d'attente",
        source: "Source",
        target: "Cible",
        searchLanguage: "Rechercher une langue...",
        detection: "Détection",
        favorites: "Favoris",
        allLanguages: "Toutes les langues",
        loading: "Chargement...",
        textField: "Champ texte",
        textResult: "Résultat",
        text: "Fichier TXT",
        markdown: "Markdown",
        officeDoc: "Document Office",
        pptxFile: "Fichier PPTX",
        csvFile: "Fichier CSV",
        pdf: "PDF",
        textFile: "Fichier TXT",
        markdownFile: "Fichier Markdown",
        translateInput: "Traduire",
        pause: "Pause",
        resume: "Reprendre",
        stop: "Arrêter",
        cancel: "Annuler",
        historySelect: "Sélectionner",
        historySelectAll: "Tout sélectionner",
        historyDeleteCount: "Supprimer ({count})",
        history: "Historique",
        noHistory: "Aucune traduction enregistrée.",
        noHistoryMatch: "Aucun élément de l'historique ne correspond à ce filtre.",
        pageInfo: "Page {page} / {total}",
        queued: "En file",
        statusRunning: "en cours",
        statusPaused: "en pause",
        statusComplete: "terminé",
        statusFailed: "échoué",
        statusCancelled: "annulé",
        queuePosition: "Position en file #{position}",
        watchJob: "Cliquer pour suivre ce job dans le titre de l'onglet.",
        unwatchJob: "Cliquer pour ne plus le suivre ; le titre revient à Linguinator seul.",
        started: "Démarré",
        chunks: "segments",
        jobFailed: "Le job a échoué.",
        selectFileFirst: "Sélectionne d'abord un fichier compatible texte, document, tableau, sous-titres ou localisation.",
        selectPdfFirst: "Sélectionne d'abord un PDF.",
        extracting: "Extraction",
        starting: "Démarrage...",
        startingChunks: "Démarrage de {count} segments...",
        uploadingPdf: "Téléversement du PDF...",
        formatTxt: "TXT",
        formatMarkdown: "Markdown",
        formatPdf: "PDF simple",
        formatDoc: "Doc simple",
        formatOriginal: "Format original",
        download: "Télécharger",
        downloadItem: "Télécharger cet élément de l'historique.",
        historyFormat: "Choisir le format de téléchargement de l'historique.",
        selectLanguage: "Sélectionner {language}.",
        pageRangeHint: "Pages, p. ex. 1-3,5",
        sheetNameLabel: "Feuille XLSX",
        sheetNameHint: "Optionnel, première feuille si vide",
        csvColumnsLabel: "Colonnes à traduire",
        csvColumnsHint: "Optionnel, p. ex. titre,description ou A,B",
        historyFilterHint: "Filtrer l'historique...",
        tipReload: "Recharger Linguinator.",
        tipUiLanguage: "Choisir la langue de l'interface.",
        tipTheme: "Basculer entre mode clair et sombre.",
        tipTabTextarea: "Saisir le texte directement dans le champ.",
        tipTabText: "Charger et traduire un fichier TXT, ainsi que HTML, SRT, VTT, JSON, YAML, PO ou XLIFF.",
        tipTabMarkdown: "Charger et traduire un fichier Markdown.",
        tipTabOffice: "Charger et traduire un fichier DOCX ou ODT comme texte.",
        tipTabPptx: "Charger et traduire un fichier PowerPoint (PPTX) comme texte.",
        tipTabCsv: "Charger et traduire des colonnes CSV ou XLSX choisies.",
        tipTabPdf: "Extraire et traduire un PDF page par page.",
        tipInputType: "Choisir le type d'entrée.",
        tipSource: "Choisir la langue source.",
        tipTarget: "Choisir la langue cible.",
        tipTextarea: "Saisir le texte ou le charger depuis un fichier.",
        tipCounter: "Caractères utilisés, limite et blocs estimés.",
        tipTextResult: "La traduction, une fois le travail terminé.",
        tipFilePick: "Choisir le fichier à charger.",
        tipSheetName: "Quelle feuille utiliser dans un fichier XLSX. Vide pour la première feuille. Pas utilisé pour les fichiers CSV, ni si le champ colonnes ci-dessous indique ses propres feuilles.",
        tipCsvColumns: "Noms ou lettres des colonnes à traduire. Laisser vide pour traduire chaque cellule (nombres, adresses IP et MAC ignorés automatiquement). Pour plusieurs feuilles XLSX à la fois : une ligne \"Feuille : colonnes\" par feuille, qui remplace le champ feuille ci-dessus.",
        tipPdfPick: "Choisir le PDF à traduire.",
        tipPageRange: "Plage de pages optionnelle, p. ex. 1-3,5. Vide traduit tout.",
        tipTranslate: "Lancer une traduction pour l'entrée actuelle.",
        tipPause: "Mettre toute la file en pause.",
        tipResume: "Reprendre toute la file.",
        tipStop: "Annuler toute la file.",
        tipHistoryReset: "Sélectionner des entrées de l'historique à supprimer.",
        tipHistoryToggle: "Afficher ou masquer l'historique.",
        tipHistoryFilter: "Filtrer l'historique par nom ou langues.",
        tipPrevPage: "Page précédente.",
        tipNextPage: "Page suivante.",
        tipRepo: "Ouvrir le dépôt de Linguinator.",
        modelDedicated: "Modèle dédié pour cette paire de langues.",
        modelFallback: "Pas de modèle dédié pour cette paire, utilise le modèle multilingue.",
        modelAutoDetect: "Détectée automatiquement. Les scans prennent plus de temps; si deux écritures se mélangent, choisis la langue.",
        autoDetect: "Détection automatique",
      }
    };
    const AUTO_LANGUAGE = {code: "auto", name: "auto"};
    // Names follow the UI language, so this is rebuilt whenever that changes. The English names
    // stay available for the menu search: someone who learned the list in English should still
    // find "Spanish" after switching the interface to German.
    let languageNames = new Intl.DisplayNames([currentUiLanguage], {type: "language"});
    const englishLanguageNames = new Intl.DisplayNames(["en"], {type: "language"});
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

    // job.status is the server's own internal word (queued/running/paused/complete/failed/
    // cancelled) and stays that way regardless of UI language - every place that shows it to a
    // reader goes through this instead of the raw value, or it is stuck in English no matter
    // which language is picked.
    const STATUS_KEYS = {
      queued: "queued", running: "statusRunning", paused: "statusPaused",
      complete: "statusComplete", failed: "statusFailed", cancelled: "statusCancelled",
    };

    function translateStatus(status) {
      return t(STATUS_KEYS[status] || status);
    }

    function setText(selector, key) {
      const element = document.querySelector(selector);
      if (element) element.textContent = t(key);
    }

    function setPlaceholder(selector, key) {
      const element = document.querySelector(selector);
      if (element) element.placeholder = t(key);
    }

    function setTitle(selector, key) {
      const element = document.querySelector(selector);
      if (element) element.title = t(key);
    }

    function applyUiLanguage() {
      document.documentElement.lang = currentUiLanguage;
      try {
        languageNames = new Intl.DisplayNames([currentUiLanguage], {type: "language"});
      } catch {
        languageNames = new Intl.DisplayNames(["en"], {type: "language"});
      }
      const uiLanguage = document.getElementById("uiLanguage");
      if (uiLanguage) uiLanguage.value = currentUiLanguage;
      setText(".subtle", "subtitle");
      setText('label[for="uiLanguage"]', "uiLanguage");
      setText('label[for="source"]', "source");
      setText('label[for="target"]', "target");
      setText('[data-input-tab="textarea"] .tab-label', "textField");
      setText('[data-input-tab="text"] .tab-label', "text");
      setText('[data-input-tab="markdown"] .tab-label', "markdown");
      setText('[data-input-tab="office"] .tab-label', "officeDoc");
      setText('[data-input-tab="pptx"] .tab-label', "pptxFile");
      setText('[data-input-tab="csv"] .tab-label', "csvFile");
      setText('[data-input-tab="pdf"] .tab-label', "pdf");
      setText('label[for="text"]', "textField");
      setText('label[for="textResult"]', "textResult");
      setText('label[for="pdf"]', "pdf");
      setText("#translate", "translateInput");
      setText("#pauseJob", "pause");
      setText("#resumeJob", "resume");
      setText("#stopJob", "stop");
      updateHistoryDeleteButton();
      setText("#historySelectAllLabel span", "historySelectAll");
      setText('label[for="history"]', "history");
      setTitle("#uiLanguage", "uiLanguage");
      for (const element of document.querySelectorAll("[data-title-key]")) {
        element.title = t(element.dataset.titleKey);
      }
      setText('label[for="sheetName"]', "sheetNameLabel");
      setText('label[for="csvColumns"]', "csvColumnsLabel");
      setPlaceholder("#pageRange", "pageRangeHint");
      setPlaceholder("#sheetName", "sheetNameHint");
      setPlaceholder("#csvColumns", "csvColumnsHint");
      setPlaceholder("#historyFilter", "historyFilterHint");
      refreshInputLabels();
      refreshInputTabSelectLabels();
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
    }

    function getFavoriteLanguages() {
      // Set per installation via LINGUINATOR_FAVORITE_LANGUAGES, English always included.
      // Still filtered against the languages on offer: the setting can name one that a later
      // release dropped, and an entry with no language behind it would render as an empty row.
      return (languageData.favorites || []).filter((code) => Boolean(languageByCode(code)));
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
      // Intl first now: it knows the name in the UI language, the fallback table is English only
      // and covers the codes Intl does not know.
      let name = intlName || languageFallbacks[languagePart] || languagePart;
      if (name === languagePart) {
        name = titleCase(languagePart);
      }
      const flag = countryFlag(languageCountries[languagePart]);
      return {name, flag};
    }

    function englishLanguageName(languageCode) {
      const languagePart = languageCode.split("_")[0];
      let intlName = "";
      try {
        intlName = englishLanguageNames.of(languagePart) || "";
      } catch {
        intlName = "";
      }
      return languageFallbacks[languagePart] || intlName || languagePart;
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

    function languagesByDisplayName() {
      // The server hands the list back sorted by language code, which puts Greek under "ell"
      // and Dutch under "nld". The menu shows names, so it sorts by name.
      return [...languageData.languages].sort((a, b) =>
        languageDisplayName(a.code).name.localeCompare(
          languageDisplayName(b.code).name, currentUiLanguage));
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
      option.dataset.english = englishLanguageName(language.code).toLowerCase();
      option.title = t("selectLanguage").replace("{language}", formatLanguageLabel(language.code));
      option.innerHTML =
        '<span class="language-name">' + escapeHtml(formatLanguageLabel(language.code)) + '</span>' +
        '<span class="language-code">' + escapeHtml(language.code) + '</span>';
      option.classList.toggle("active", language.code === selectedValue);
      option.addEventListener("click", () => {
        setPickerValue(id, language.code);
        // Store the pick here rather than in setPickerValue, which also runs while the menu is
        // being built: a job start is too late, picking a language and reloading without
        // translating anything used to throw the choice away.
        saveRecent(id, language.code);
      });
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
      const favorites = favoriteCodes.map(languageByCode);
      select.innerHTML = "";
      menu.innerHTML = "";

      const ordered = languagesByDisplayName();
      if (id === "source") {
        addOption(select, AUTO_LANGUAGE);
      }
      for (const language of ordered) {
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
      for (const language of ordered) {
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
          child.textContent.toLowerCase().includes(normalized) ||
          (child.dataset.english || "").includes(normalized);
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
      if (!localStorage.getItem("linguinator_ui_language") && uiText[languageData.ui_language]) {
        currentUiLanguage = languageData.ui_language;
        applyUiLanguage();
      }
      const sourceFallback = languageCodeOrFallback(languageData.source_default, AUTO_LANGUAGE.code);
      const targetFallback = languageCodeOrFallback(languageData.target_default, "eng_Latn");
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
      localStorage.setItem("linguinator_input_tab", currentInputTab);
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
      const box = document.getElementById("textResult");
      if (box) box.value = text;
    }

    // Reads the selected file's text into #text so Translate can send it - not a preview step,
    // #text lives in a different tab-panel that stays hidden while a file tab is active, so
    // running this on its own left the old "Load File" button doing nothing the user could see.
    async function loadTextFile() {
      const file = document.getElementById("textFile").files[0];
      if (!file) {
        setStatusRow("failed", t("selectFileFirst"));
        return false;
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
        setStatusRow("extracting", t("extracting") + " " + lowerName.split(".").pop().toUpperCase() + "...");
        const response = await fetch(path, {method: "POST", body: form});
        const text = await response.text();
        if (!response.ok) {
          setStatusRow("failed", errorTextFromResponse(text));
          return false;
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
      clearStatusRow();
      return true;
    }


    function clearOwnJobOnNewFile() {
      // Drops the green marking off the previous job's history row: a new file means the last
      // result is no longer what the user is working on.
      lastCompletedJob = null;
      renderHistory();
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

    // Work that has no job of its own yet - extracting a file, uploading, fetching a page - and
    // every error message. Shown as the first row of the queue, in the shape of a job row, since
    // that is where the eye already is once something is running.
    let statusRow = null;

    function setStatusRow(status, info) {
      statusRow = {status, info};
      if (status !== "failed") document.title = titleFor(status) + " - " + baseTitle;
      openJobsPanel();
      renderQueueRows();
    }

    function clearStatusRow() {
      statusRow = null;
      document.title = baseTitle;
      renderQueueRows();
    }

    function titleFor(status, percent, position, label) {
      const titleStatus = translateStatus(status);
      let prefix;
      if ((status === "queued" || status === "running") && position && position > 0) {
        prefix = "#" + position + " " + titleStatus;
      } else if (percent === undefined) {
        prefix = titleStatus;
      } else {
        prefix = Math.max(0, Math.min(100, Number(percent) || 0)) + "% " + titleStatus;
      }
      return prefix + (label ? " - " + label : "");
    }

    // The tab title still follows the job being watched, which is the one thing a row in the
    // list cannot do for a tab in the background.
    function updateDocumentTitle(job) {
      const position = job.position && job.position > 0 ? job.position : undefined;
      document.title =
        titleFor(job.status, job.percent || 0, position, job.label || job.kind) + " - " + baseTitle;
    }

    async function loadHealth() {
      const response = await fetch("health");
      const data = await response.json();
      maxChars = data.max_chars || 0;
      historyTimezone = data.timezone || "UTC";
      timeFormat = data.time_format === "12h" ? "12h" : "24h";
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
      // Set per installation, the same for every UI language.
      return timeFormat === "12h";
    }

    function formatJobTime(timestamp) {
      if (!timestamp) return "";
      return new Date(timestamp * 1000).toLocaleString(undefined, {hour12: hour12Option()});
    }

    async function controlJob(jobId, action) {
      const response = await fetch("jobs/" + jobId + "/" + action, {method: "POST"});
      if (!response.ok) {
        const text = await response.text();
        setStatusRow("failed", errorTextFromResponse(text));
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
          updateDocumentTitle(updated);
        }
      });
      return button;
    }

    function queueRing(job) {
      const ring = document.createElement("div");
      const failed = job.status === "failed";
      // Nothing to count yet: the file is still being read or uploaded, so the ring turns
      // instead of filling.
      const spinning = job.status === "extracting";
      ring.className = "queue-ring" + (spinning ? " indeterminate" : "");
      // A quarter of the ring is what turns; a full or empty one would show no movement at all.
      const percent = failed ? 100
        : spinning ? 25 : Math.max(0, Math.min(100, Number(job.percent) || 0));
      // 2 * PI * r, with r = 16 in the 36x36 viewBox the circles are drawn in.
      const circumference = 100.53;
      let label;
      if (failed) label = "!";
      else if (spinning) label = "";
      else if (job.status === "queued" && job.position) label = "#" + job.position;
      else label = Math.round(percent);
      ring.innerHTML =
        '<svg viewBox="0 0 36 36" aria-hidden="true">' +
          '<circle class="queue-ring-track" cx="18" cy="18" r="16"/>' +
          '<circle class="queue-ring-fill" cx="18" cy="18" r="16" stroke-dasharray="' +
            (percent / 100 * circumference).toFixed(2) + ' ' + circumference + '"/>' +
        '</svg>' +
        '<span class="queue-ring-label">' + escapeHtml(label) + '</span>';
      return ring;
    }

    function queueDivider() {
      const divider = document.createElement("div");
      divider.className = "queue-divider";
      return divider;
    }

    function statusRowElement() {
      const row = document.createElement("div");
      row.className = "queue-row status-" + statusRow.status;
      // The message alone, in the title line: the ring already says whether this is work in
      // progress or a failure, and the raw status word ("failed") is not translated anyway.
      const main = document.createElement("div");
      const title = document.createElement("div");
      title.className = "queue-title";
      title.textContent = statusRow.info;
      main.appendChild(title);
      row.appendChild(queueRing(statusRow));
      row.appendChild(main);
      return row;
    }

    async function loadQueue() {
      const response = await fetch("jobs");
      if (!response.ok) return;
      const data = await response.json();
      queueItems = data.items;
      let hasNewlyCompleted = false;
      for (const job of data.items) {
        if (job.status === "complete" && !seenCompletedJobIds.has(job.id)) {
          seenCompletedJobIds.add(job.id);
          hasNewlyCompleted = true;
        }
      }
      if (hasNewlyCompleted) loadHistory();
      renderQueueRows();
    }

    function renderQueueRows() {
      const queue = document.getElementById("queue");
      queue.innerHTML = "";
      // A finished job leaves the queue at once: it is in the history right below, marked
      // green there, and standing in both lists at the same time read as a duplicate.
      const activeItems = queueItems.filter((job) => !["complete", "failed", "cancelled"].includes(job.status));
      updateQueueControlButtons(activeItems);
      // Only when work appears, not for as long as it lasts: reopening it on every poll would
      // make the panel impossible to close while a job runs.
      if (activeItems.length && !hadActiveJobs) openJobsPanel();
      hadActiveJobs = activeItems.length > 0;
      if (statusRow) queue.appendChild(statusRowElement());
      for (const job of activeItems) {
        // A rule where the work stops and the waiting starts, but only if something stands
        // above it - a queue whose first row is already waiting has nothing to divide.
        if (job.status === "queued" && queue.lastElementChild
            && !queue.lastElementChild.classList.contains("status-queued")) {
          queue.appendChild(queueDivider());
        }
        const row = document.createElement("div");
        row.className = "queue-row status-" + job.status;
        // The row's own background is the progress bar: it fills up to here.
        if (job.status === "running") {
          row.style.setProperty("--fill", Math.max(0, Math.min(100, Number(job.percent) || 0)) + "%");
        }
        const main = document.createElement("div");
        const title = document.createElement("div");
        title.className = "queue-title";
        const extension = jobExtension(job);
        row.dataset.fileType = fileTypeKey(extension, job.label);
        appendTypeBadge(title, extension, job.label);
        const position = job.position ? "#" + job.position + " " : "";
        title.appendChild(document.createTextNode(
          position + (job.label || job.kind) + " - " + translateStatus(job.status)));
        const meta = document.createElement("div");
        meta.className = "queue-meta";
        const languages = [job.source, job.target].filter(Boolean).map(formatLanguageLabel).join(" -> ");
        const started = job.started_at ? t("started") + " " + formatJobTime(job.started_at) : t("queued") + " " + formatJobTime(job.queued_at);
        meta.appendChild(document.createTextNode([languages, started].filter(Boolean).join(" | ")));
        const progress = document.createElement("div");
        progress.className = "queue-progress";
        const progressLabel = job.status === "queued" && job.position
          ? t("queuePosition", {position: job.position})
          : (job.percent || 0) + "%";
        // eta_seconds is 0 once every chunk is translated, even while a PDF job still has to
        // render the result - "ETA 0s" next to "99%" and "Rendering PDF" reads as finished
        // when it is not, so this only shows a real countdown, not the leftover zero.
        const eta = job.status === "running" && job.eta_seconds
          ? "ETA " + formatEta(job.eta_seconds) : "";
        progress.textContent = [
          progressLabel,
          (job.current || 0) + " / " + (job.total || 0) + " " + t("chunks"),
          eta,
          job.message || "",
        ].filter(Boolean).join(" | ");
        main.appendChild(title);
        main.appendChild(meta);
        main.appendChild(progress);
        row.appendChild(queueRing(job));
        row.appendChild(main);
        const actions = document.createElement("div");
        actions.className = "queue-actions";
        actions.appendChild(queueActionButton(job, "cancel", t("cancel"), ["queued", "running", "paused"]));
        row.appendChild(actions);
        row.classList.toggle("watched", job.id === activeJobId);
        row.title = t("watchJob");
        row.addEventListener("click", (event) => {
          if (event.target.closest(".queue-actions")) return;
          watchJob(job.id);
        });
        queue.appendChild(row);
      }
      // And one where the queue ends and the finished translations begin, so the two groups stay
      // apart even once nothing is waiting any more.
      if (queue.lastElementChild && historyItems.length) queue.appendChild(queueDivider());
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

    function unwatchJob() {
      // Bumping the token stops whatever pollJob loop is running, so no later answer of its own
      // puts the title back.
      pollToken += 1;
      activeJobId = null;
      document.title = baseTitle;
      renderQueueRows();
    }

    function watchJob(jobId) {
      ensureAudioContext();
      activeJobId = jobId;
      const token = ++pollToken;
      loadQueue().catch(() => {});
      pollJob(jobId, token);
    }

    async function pollJob(jobId, token) {
      // The job has a row of its own from here on; the status row was only standing in for it
      // while it was being uploaded and queued.
      statusRow = null;
      while (token === pollToken) {
        const response = await fetch("jobs/" + jobId);
        if (!response.ok) {
          const text = await response.text();
          if (token === pollToken) {
            setStatusRow("failed", errorTextFromResponse(text));
            activeJobId = null;
          }
          return;
        }
        const job = await response.json();
        if (token !== pollToken) return;
        updateDocumentTitle(job);
        if (job.status === "complete") {
          // Every other job kind's result is a whole document (or several pages of markdown) -
          // fine for History's download, too much for a small inline box meant for a sentence
          // or two typed into the Text Field tab.
          setResult(job.kind === "translate" ? (job.result || "") : "");
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
          setStatusRow("failed", job.error || t("jobFailed"));
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
      const sourceFile = currentInputTab !== "textarea" ? document.getElementById("textFile").files[0] : null;
      if (sourceFile && !(await loadTextFile())) return;
      const source = document.getElementById("source").value;
      const target = document.getElementById("target").value;
      const length = document.getElementById("text").value.length;
      const chunks = maxChars > 0 ? Math.max(1, Math.ceil(length / maxChars)) : 1;
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
      setStatusRow("queued", chunks > 1 ? t("startingChunks", {count: chunks}) : t("starting"));
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
        setStatusRow("failed", errorTextFromResponse(text));
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
        setStatusRow("failed", t("selectPdfFirst"));
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
      setStatusRow("extracting", t("uploadingPdf"));
      // Always the layout-preserving job: it falls back to the plain/OCR pipeline server-side
      // for scanned PDFs. Plain output stays available afterwards via the TXT/Markdown/PDF/Doc
      // download buttons, which work from the stored result text regardless of which pipeline ran.
      const response = await fetch("jobs/translate-pdf-layout", {method: "POST", body: form});
      if (!response.ok) {
        const text = await response.text();
        setStatusRow("failed", errorTextFromResponse(text));
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
        if (activeResponse.ok) updateDocumentTitle(await activeResponse.json());
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

    function historyTimestamp(item) {
      const date = formatHistoryDate(item.created_at);
      const time = formatHistoryTime(item.created_at);
      return date + (time ? " " + time : "");
    }

    function formatBytes(bytes) {
      if (!bytes) return "0 B";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
      return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    // Picking entries to delete, rather than "keep the N most recent": the button doubles as the
    // mode switch (Select -> Delete (N) / Cancel) so there is no separate popover to open, close
    // or click outside of.
    let historySelecting = false;
    let selectedHistoryIds = new Set();

    function updateHistoryDeleteButton() {
      const button = document.getElementById("resetHistory");
      const selectAllLabel = document.getElementById("historySelectAllLabel");
      if (!historySelecting) {
        button.textContent = t("historySelect");
        selectAllLabel.classList.add("invisible");
        return;
      }
      selectAllLabel.classList.remove("invisible");
      button.textContent = selectedHistoryIds.size
        ? t("historyDeleteCount", {count: selectedHistoryIds.size})
        : t("cancel");
      const allIds = filteredHistoryItems().map((item) => item.id);
      document.getElementById("historySelectAll").checked =
        allIds.length > 0 && allIds.every((id) => selectedHistoryIds.has(id));
    }

    function toggleHistorySelection(id) {
      if (selectedHistoryIds.has(id)) selectedHistoryIds.delete(id);
      else selectedHistoryIds.add(id);
      updateHistoryDeleteButton();
    }

    function exitHistorySelection() {
      historySelecting = false;
      selectedHistoryIds.clear();
      document.getElementById("history").classList.remove("selecting");
      updateHistoryDeleteButton();
    }

    document.getElementById("historySelectAll").addEventListener("change", (event) => {
      if (event.target.checked) {
        for (const item of filteredHistoryItems()) selectedHistoryIds.add(item.id);
      } else {
        selectedHistoryIds.clear();
      }
      updateHistoryDeleteButton();
      renderHistory();
    });

    document.getElementById("resetHistory").addEventListener("click", async () => {
      if (!historySelecting) {
        historySelecting = true;
        document.getElementById("history").classList.add("selecting");
        updateHistoryDeleteButton();
        renderHistory();
        return;
      }
      if (!selectedHistoryIds.size) {
        exitHistorySelection();
        renderHistory();
        return;
      }
      const ids = [...selectedHistoryIds];
      await Promise.all(ids.map((id) => fetch("history/" + encodeURIComponent(id), {method: "DELETE"})));
      exitHistorySelection();
      await loadHistory();
    });

    async function loadHistory() {
      const response = await fetch("history");
      const data = await response.json();
      historyItems = data.items || [];
      renderHistory();
    }

    function filteredHistoryItems() {
      const needle = historyFilterText.trim().toLowerCase();
      if (!needle) return historyItems;
      return historyItems.filter((item) => {
        const haystack = [item.original_name, item.source, item.target].join(" ").toLowerCase();
        return haystack.includes(needle);
      });
    }

    // The input tabs carry a colour per file type (see styles.css); a row's badge borrows it, so
    // the same kind of document looks the same wherever it shows up.
    const FILE_TYPE_GROUPS = {
      pdf: "pdf", docx: "office", odt: "office", pptx: "pptx",
      csv: "csv", xlsx: "csv", md: "markdown",
    };

    // A job carries no file extension of its own (create_job does not store one), so the badge
    // reads it off the filename the way the server does for history entries. No name to read and
    // no PDF job behind it means no badge at all - "TXT" on a PDF was worse than nothing.
    function jobExtension(job) {
      if (String(job.kind || "").startsWith("translate-pdf")) return "pdf";
      const named = /\.([A-Za-z0-9]{1,12})$/.exec(job.label || "");
      return named ? named[1].toLowerCase() : "";
    }

    // The Text Field tab always submits under the fixed name "text.txt" (see /jobs/translate),
    // the only thing that tells its badge apart from an actually uploaded .txt file.
    function isTextFieldName(name) {
      return String(name || "").toLowerCase() === "text.txt";
    }

    // "txt" isn't in FILE_TYPE_GROUPS: an uploaded .txt file falls through to the "text" group
    // (the TXT File tab's own colour), while the Text Field tab's fixed "text.txt" name keeps
    // the plainer "textarea" grouping instead.
    function fileTypeKey(extension, name) {
      const key = String(extension || "").toLowerCase().replace(/^\./, "");
      if (!key) return "textarea";
      if (key === "txt" && isTextFieldName(name)) return "textarea";
      return FILE_TYPE_GROUPS[key] || "text";
    }

    function typeBadgeElement(extension, name) {
      if (!extension) return null;
      const badge = document.createElement("span");
      badge.className = "queue-type-badge";
      badge.textContent = extension === "txt" && isTextFieldName(name) ? "TEXT" : String(extension).toUpperCase();
      return badge;
    }

    function appendTypeBadge(parent, extension, name) {
      const badge = typeBadgeElement(extension, name);
      if (badge) parent.appendChild(badge);
    }

    function buildHistoryRow(item) {
      const row = document.createElement("div");
      row.className = "history-row";
      if (lastCompletedJob && lastCompletedJob.history_id === item.id) {
        row.classList.add("history-row-own-current");
      }
      // Same left column as the queue rows above, where a finished job carries the same tick -
      // swapped for a checkbox in the same slot while deleting is in progress.
      const tick = document.createElement("span");
      tick.className = "history-tick";
      tick.textContent = "✓";
      const select = document.createElement("input");
      select.type = "checkbox";
      select.className = "history-select";
      select.checked = selectedHistoryIds.has(item.id);
      select.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleHistorySelection(item.id);
      });
      row.dataset.fileType = fileTypeKey(item.source_extension, item.original_name);
      const main = document.createElement("div");
      main.className = "history-main";
      const title = document.createElement("div");
      title.className = "history-title";
      const link = document.createElement("a");
      // The name on its own line; everything that describes it - type, languages, size, when -
      // on the line below, so a long filename cannot push any of it out of sight.
      link.textContent = item.original_name;
      const meta = document.createElement("div");
      meta.className = "history-meta";
      // Names, not codes: "deu_Latn -> eng_Latn" is unreadable at a glance in a long list.
      meta.appendChild(document.createTextNode(
        formatLanguageLabel(item.source) + " -> " + formatLanguageLabel(item.target)
        + " | " + formatBytes(item.size_bytes)
        + " | " + historyTimestamp(item)));
      const format = document.createElement("select");
      format.className = "history-format";
      format.title = t("historyFormat");
      const hasOriginal = item.has_source_file && item.source_extension;
      // Plain PDF is either worse than the original-format re-export (layout mode dropped) or,
      // without layout mode, byte-for-byte the same output (history_original_export falls back to
      // the same create_text_pdf) - offering it beside "Original Format (.pdf)" only invites
      // picking the redundant one.
      // The Text Field tab's own output isn't a document with layout to preserve or reflow,
      // so Markdown and Plain PDF (both meant for structured files) don't apply to it.
      // An uploaded .txt or .md file re-exports as "Original Format" byte-for-byte the same as
      // Markdown/Plain TXT (history_original_export just writes the text back out for either
      // extension) - offering all three beside each other only invites picking a redundant one.
      const isPlainTextUpload = ["txt", "md"].includes(item.source_extension) && !isTextFieldName(item.original_name);
      const historyFormats = item.source_extension === "pdf"
        ? ["md", "txt", "doc"]
        : item.source_extension === "docx"
        ? ["txt"]
        : item.source_extension === "pptx" || item.source_extension === "csv"
        ? (hasOriginal ? [] : ["txt"])
        : isTextFieldName(item.original_name)
        ? ["txt", "doc"]
        : isPlainTextUpload && hasOriginal
        ? ["pdf", "doc"]
        : ["md", "txt", "pdf", "doc"];
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
      download.title = t("downloadItem");
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
      appendTypeBadge(title, item.source_extension, item.original_name);
      title.appendChild(link);
      main.appendChild(title);
      main.appendChild(meta);
      const actions = document.createElement("div");
      actions.className = "history-actions";
      actions.appendChild(format);
      actions.appendChild(download);
      row.appendChild(tick);
      row.appendChild(select);
      row.appendChild(main);
      row.appendChild(actions);
      // Clicking a running job in the list above attaches it to this browser's tab title;
      // clicking a finished one here lets go of it again. The marking on the row stays, it says
      // who started the job, not what the tab is showing. While deleting is in progress, a row
      // click toggles its checkbox instead - the whole row is the easier target to hit.
      row.title = historySelecting ? "" : t("unwatchJob");
      row.addEventListener("click", (event) => {
        if (event.target.closest(".history-actions") || event.target.closest("a")) return;
        if (historySelecting) {
          toggleHistorySelection(item.id);
          renderHistory();
          return;
        }
        unwatchJob();
      });
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

    const JOBS_COLLAPSED_KEY = "linguinator_jobs_collapsed";

    // Opened whenever there is something to see, never closed: with the progress bar gone this
    // list is the only place a running job shows up, and a collapsed panel would hide it. Anyone
    // who closes it while nothing is running keeps it closed.
    function openJobsPanel() {
      const body = document.getElementById("jobsBody");
      if (!body || !body.classList.contains("hidden")) return;
      setPanelCollapsed("jobsBody", "jobsCollapseIcon", JOBS_COLLAPSED_KEY, false);
    }

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
      // which for this panel means collapsed.
      const stored = localStorage.getItem(storageKey);
      const collapsed = stored === null ? collapsedByDefault : stored === "1";
      setPanelCollapsed(bodyId, iconId, storageKey, collapsed);
    }

    setupPanelCollapse("jobsCollapse", "jobsBody", "jobsCollapseIcon", JOBS_COLLAPSED_KEY, true);

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
      setStatusRow("failed", error.toString());
    });
    loadHealth().catch((error) => {
      setStatusRow("failed", error.toString());
    });
    loadHistory().catch((error) => {
      document.getElementById("history").textContent = error.toString();
    });
    loadQueue().catch(() => {});
    setInterval(() => {
      loadQueue().catch(() => {});
    }, 3000);
    // Restored only if that tab is still enabled - a stored tab from before it was disabled
    // again (or before it was ever enabled) must not switch to a panel the user can't reach
    // through the tab bar. Checked against inputTabs first so a stray localStorage value never
    // reaches querySelector as a raw attribute selector.
    const storedInputTab = localStorage.getItem("linguinator_input_tab");
    const storedInputTabButton = storedInputTab && inputTabs[storedInputTab]
      && document.querySelector('[data-input-tab="' + storedInputTab + '"]');
    setInputTab(storedInputTabButton && !storedInputTabButton.disabled ? storedInputTab : "pdf");
    applyUiLanguage();
