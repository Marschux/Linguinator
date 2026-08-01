    let languageData = null;
    let maxChars = 0;
    let activeJobId = null;
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

    function recentKey(id) {
      return "nllb_recent_" + id;
    }

    function getRecent(id) {
      try {
        return JSON.parse(localStorage.getItem(recentKey(id)) || "[]");
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

    function extractionPathForFile(fileName) {
      if (fileName.endsWith(".csv")) return "/extract-csv";
      if (fileName.endsWith(".xlsx")) return "/extract-xlsx";
      if (fileName.endsWith(".odt")) return "/extract-odt";
      return "/extract-docx";
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
      const response = await fetch("/languages");
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

    function setResult(text) {
      const result = document.getElementById("result");
      const hasText = Boolean(text.trim());
      result.textContent = text;
      document.getElementById("downloadResult").disabled = !hasText;
      document.getElementById("downloadTextResult").disabled = !hasText;
    }

    function downloadResult(extension, contentType) {
      const text = document.getElementById("result").textContent;
      if (!text.trim()) return;
      const blob = new Blob([text], {type: contentType + ";charset=utf-8"});
      const link = document.createElement("a");
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      const url = URL.createObjectURL(blob);
      link.href = url;
      link.download = "nllb-result-" + stamp + "." + extension;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    }

    async function loadTextFile() {
      const file = document.getElementById("textFile").files[0];
      if (!file) {
        setResult("Select a TXT, Markdown, DOCX, ODT, CSV, or XLSX file first.");
        return;
      }
      const lowerName = file.name.toLowerCase();
      const isCsv = lowerName.endsWith(".csv");
      const isXlsx = lowerName.endsWith(".xlsx");
      const isOfficeFile = lowerName.endsWith(".docx") || lowerName.endsWith(".odt");
      if (isCsv || isXlsx || isOfficeFile) {
        const form = new FormData();
        form.append("file", file);
        if (isCsv || isXlsx) {
          form.append("columns", document.getElementById("csvColumns").value);
        }
        if (isXlsx) {
          form.append("sheet_name", document.getElementById("sheetName").value);
        }
        const path = extractionPathForFile(lowerName);
        showProgress("queued", 0, "Extracting " + lowerName.split(".").pop().toUpperCase() + "...");
        const response = await fetch(path, {method: "POST", body: form});
        const text = await response.text();
        if (!response.ok) {
          setResult(errorTextFromResponse(text));
          return;
        }
        document.getElementById("text").value = text;
      } else {
        document.getElementById("text").value = await file.text();
      }
      updateCounter();
      setResult("");
      clearProgress();
    }

    document.getElementById("downloadResult").addEventListener("click", () => downloadResult("md", "text/markdown"));
    document.getElementById("downloadTextResult").addEventListener("click", () => downloadResult("txt", "text/plain"));
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
      progress.className = "progress";
      progress.innerHTML = "";
    }

    async function loadHealth() {
      const response = await fetch("/health");
      const data = await response.json();
      maxChars = data.max_chars || 0;
      const ocr = document.getElementById("useOcr");
      const ocrLabel = document.getElementById("ocrLabel");
      ocr.checked = Boolean(data.ocr_enabled);
      ocr.disabled = true;
      ocrLabel.textContent = data.ocr_enabled
        ? "OCR configured: " + data.ocr_language
        : "OCR disabled";
      updateCounter();
    }

    function updateCounter() {
      const counter = document.getElementById("counter");
      const length = document.getElementById("text").value.length;
      const chunks = maxChars > 0 ? Math.max(1, Math.ceil(length / maxChars)) : 1;
      counter.textContent = length + " / " + maxChars + " (" + chunks + " chunk" + (chunks === 1 ? "" : "s") + ")";
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
        const response = await fetch("/jobs/" + jobId);
        if (!response.ok) {
          const text = await response.text();
          setResult(errorTextFromResponse(text));
          activeJobId = null;
          return;
        }
        const job = await response.json();
        updateProgress(job);
        if (job.status === "complete") {
          setResult(job.result || "");
          loadHistory();
          activeJobId = null;
          return;
        }
        if (job.status === "failed") {
          setResult(job.error || "Job failed.");
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
      saveRecent("source", source);
      saveRecent("target", target);
      renderSelect("source", source);
      renderSelect("target", target);
      setResult("");
      showProgress("queued", 0, chunks > 1 ? "Starting " + chunks + " chunks..." : "Starting...");
      const response = await fetch("/jobs/translate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          q: document.getElementById("text").value,
          source: source,
          target: target
        })
      });
      if (!response.ok) {
        const text = await response.text();
        setResult(errorTextFromResponse(text));
        clearProgress();
        return;
      }
      const data = await response.json();
      activeJobId = data.job_id;
      await pollJob(data.job_id);
    }

    document.getElementById("translate").addEventListener("click", startTextJob);

    async function postPdf(path) {
      const file = document.getElementById("pdf").files[0];
      if (!file) {
        setResult("Select a PDF first.");
        return;
      }
      const form = new FormData();
      form.append("file", file);
      form.append("source", document.getElementById("source").value);
      form.append("target", document.getElementById("target").value);
      form.append("page_range", document.getElementById("pageRange").value);
      setResult(path === "/translate-pdf" ? "Extracting and translating PDF..." : "Extracting PDF...");
      const response = await fetch(path, {method: "POST", body: form});
      const text = await response.text();
      setResult(response.ok ? text : errorTextFromResponse(text));
    }

    async function postPdfJob() {
      const file = document.getElementById("pdf").files[0];
      if (!file) {
        setResult("Select a PDF first.");
        return;
      }
      const source = document.getElementById("source").value;
      const target = document.getElementById("target").value;
      saveRecent("source", source);
      saveRecent("target", target);
      renderSelect("source", source);
      renderSelect("target", target);
      const form = new FormData();
      form.append("file", file);
      form.append("source", source);
      form.append("target", target);
      form.append("page_range", document.getElementById("pageRange").value);
      setResult("");
      showProgress("queued", 0, "Uploading PDF...");
      const response = await fetch("/jobs/translate-pdf", {method: "POST", body: form});
      if (!response.ok) {
        const text = await response.text();
        setResult(errorTextFromResponse(text));
        clearProgress();
        return;
      }
      const data = await response.json();
      activeJobId = data.job_id;
      await pollJob(data.job_id);
    }

    async function controlActiveJob(action) {
      if (!activeJobId) return;
      const response = await fetch("/jobs/" + activeJobId + "/" + action, {method: "POST"});
      if (!response.ok) {
        const text = await response.text();
        setResult(errorTextFromResponse(text));
        return;
      }
      const job = await response.json();
      updateProgress(job);
    }

    function formatBytes(bytes) {
      if (!bytes) return "0 B";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + " KB";
      return (bytes / 1024 / 1024).toFixed(1) + " MB";
    }

    async function deleteHistoryItem(id) {
      const response = await fetch("/history/" + id, {method: "DELETE"});
      if (!response.ok) {
        const text = await response.text();
        setResult(errorTextFromResponse(text));
        return;
      }
      await loadHistory();
    }

    async function loadHistory() {
      const response = await fetch("/history");
      const data = await response.json();
      const history = document.getElementById("history");
      history.innerHTML = "";
      if (!data.items.length) {
        history.textContent = "No saved translations yet.";
        return;
      }
      for (const item of data.items) {
        const row = document.createElement("div");
        row.className = "history-row";
        const main = document.createElement("div");
        main.className = "history-main";
        const link = document.createElement("a");
        link.href = "/history/" + item.id;
        link.textContent = item.filename;
        link.download = item.filename;
        const meta = document.createElement("div");
        meta.className = "history-meta";
        meta.textContent = item.source + " -> " + item.target + " | " + formatBytes(item.size_bytes) + " | " + item.created_at;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "secondary history-delete";
        remove.textContent = "Delete";
        remove.addEventListener("click", () => deleteHistoryItem(item.id));
        main.appendChild(link);
        main.appendChild(meta);
        row.appendChild(main);
        row.appendChild(remove);
        history.appendChild(row);
      }
    }

    document.getElementById("extractPdf").addEventListener("click", () => postPdf("/extract-pdf"));
    document.getElementById("translatePdf").addEventListener("click", postPdfJob);
    document.getElementById("pauseJob").addEventListener("click", () => controlActiveJob("pause"));
    document.getElementById("resumeJob").addEventListener("click", () => controlActiveJob("resume"));
    document.getElementById("stopJob").addEventListener("click", () => controlActiveJob("cancel"));

    loadLanguages().catch((error) => {
      setResult(error.toString());
    });
    loadHealth().catch((error) => {
      setResult(error.toString());
    });
    loadHistory().catch((error) => {
      document.getElementById("history").textContent = error.toString();
    });
