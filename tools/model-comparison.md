# Modellvergleich: Ersatz für NLLB-200

Getestet auf der Testbench (6 Kerne, 8 GB RAM, CPU only), vier Testsätze (kurzer Satz, Fachtext
aus `Stall Kamera System.pdf`, Marketing-Text aus `Get_Started_With_Smallpdf.pdf`, en→ru).
M2M100 (MIT) ist rausgeflogen: spürbar schwächere Übersetzungen (Großschreibfehler, falsche
Präpositionen, ein kompletter Unsinnssatz im Fachtext) bei gleichzeitig höherem Speicherbedarf
als NLLB. Kein Grund, das gegen die Optionen unten abzuwägen.

Erzeugt mit `tools/compare_models.py`, siehe dort für die Rohdaten/Testfälle.

## Übersicht

| Modell | Lizenz | Disk/Sprachpaar | Peak RSS | Empfehlung |
| --- | --- | --- | --- | --- |
| **OPUS-MT bilingual** (`opus-mt-en-de`, `opus-mt-de-en`, ...) | CC-BY-4.0 | ~300 MB | ~700 MB | ✅ für Standardpaare |
| **OPUS-MT mul-mul** (469 Sprachen, 1 Modell) | Apache-2.0 | 949 MB | ~1,6 GB | ✅ als Fallback |
| **MADLAD-400-3B** (ctranslate2 int8) | Apache-2.0 | 1,65 GB | ~3,6 GB | ⚠️ beste Qualität, aber halluziniert |
| NLLB-200-distilled-600M (bisheriger Default) | CC-BY-**NC** | 4,7 GB | ~2,9 GB | Referenz, kommerziell nicht nutzbar |

## OPUS-MT bilingual (en-de / de-en)

Ein Modell pro festem Sprachpaar, ~300 MB Gewichte. Qualität deckungsgleich mit NLLB, teils
sauberer.

- *"Please restart the camera after changing the SD card."* → **"Bitte starten Sie die Kamera
  nach dem Wechsel der SD-Karte neu."** (identisch mit NLLB)
- *Fachtext (de→en):* **"I would interrupt the camera purchase into several parts so that you
  can watch on the one hand, at every phase, whether it works or you need more."**
- *Marketing-Text:* **"Willkommen bei Smallpdf. Bereit, das Dokumentenmanagement auf die
  nächste Stufe zu bringen? Mit dem neuen Smallpdf-Erlebnis können Sie digitale Dokumente frei
  hochladen, organisieren und teilen."**

**Vorteil:** kleinster Ressourcenverbrauch im ganzen Vergleich, beste Qualität bei den
Standardpaaren. **Nachteil:** kein Allrounder, braucht eine kuratierte Paar-Tabelle im Code.

## OPUS-MT mul-mul (Fallback für alles außerhalb der Tabelle)

Ein einziges Modell für 469 Sprachen, 949 MB.

- *Kurzer Satz:* **"Bitte erneut starten Sie die Kamera nach dem Wechsel der SD-Karte."**
  (etwas holprige Wortstellung, aber verständlich)
- *Fachtext (de→en):* **"I wanted to interrupt the purchase of the camera in several parts so
  that you can look at one, at every stage, whether it works or you need more."**
- *Marketing-Text:* **"Willkommen bei Smallpdf. Bereit, Dokumentmanagement auf die nächste
  Stufe zu nehmen? Mit der neuen Smallpdf-Erfahrung können Sie digitale Dokumente frei
  hochladen, organisieren und teilen."**
- *en→ru:* **"Пожалуйста, перезапустите камеру после изменения SD-карты."**

**Vorteil:** deckt praktisch jede Sprachkombination ab, ohne Modell-Tabelle pflegen zu müssen.
**Nachteil:** vereinzelt holprige Formulierungen, nie aber sinnentstellend.

## MADLAD-400-3B (ctranslate2 int8) — nicht empfohlen trotz bester Rohqualität

- *Fachtext (de→en):* **"I would break the camera purchase into several parts, so that you can
  see, on the one hand, at each phase, whether it works or you need more."** (die flüssigste
  Übersetzung im ganzen Test)
- *Marketing-Text:* **"Willkommen bei Smallpdf. Sind Sie bereit, das Dokumentenmanagement auf
  die nächste Stufe zu heben? ..."** (ebenfalls sehr sauber)
- *Kurzer Satz — Problem:* **"Bitte starten Sie die Kamera nach dem Auswechseln der SD-Karte
  neu und führen Sie die folgenden Schritte aus:"** — der zweite Halbsatz ist frei erfunden,
  stand nicht im Original. **Halluzination.**

**Warum trotzdem nicht empfohlen:** höchster Ressourcenverbrauch (3,6 GB RSS, mehr als NLLB),
braucht eine zweite Laufzeitumgebung (`ctranslate2`) neben transformers, Download nur über ein
inoffizielles Drittanbieter-Repo (kein offizieller Google-Release als CTranslate2-Modell). Der
Hauptgrund ist aber die Halluzination: bei automatisiert übersetzten Dokumenten (insbesondere
kurze, strukturierte Absätze wie im Layout-PDF) prüft niemand jede Zeile gegen, erfundener Text
ist dort ein härterer Fehler als eine holprige Formulierung.

## Empfehlung

OPUS-MT bilingual für die Standardpaare (de/en/fr/es/...) + `mul-mul` als Fallback für alles
andere. Beste Qualität, mit Abstand geringster Ressourcenverbrauch, keine Halluzinationen,
saubere Lizenz (Apache-2.0 bzw. CC-BY-4.0, beide kommerziell nutzbar).
