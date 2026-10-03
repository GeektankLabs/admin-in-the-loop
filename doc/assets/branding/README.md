# Admin in the Loop – Branding-Assets für v0.0.1

Diese Datei beschreibt die Ablage und spätere README-Einbindung der ausgewählten
Logoarbeit. Sie ist eine Asset-Konvention, kein Task-Konzeptplan oder aktiver
Backlog. Die gelieferte Quell-PNG und die daraus exportierten Bilddateien liegen
in diesem Verzeichnis. Die Wort-Bild-Marke ist im Root-README eingebunden.

## Ablage und ausgewähltes Design

Branding-Bilder liegen gemeinsam unter `doc/assets/branding/`, passend zur
bestehenden Dokumentationsablage `doc/`. Dateinamen bleiben kleingeschrieben,
mit Bindestrichen und ohne Versionssuffix, damit README-Verweise bei späteren
Releases stabil bleiben.

Das ausgewählte Arbeitsdesign heißt **Admin in the Loop**: Terminalfenster bzw.
Prompt, menschliche Hand als Kontroll-/Freigabeschranke, Automatisierungs- bzw.
Leiterbahnspuren und grüner Akzent. Benötigt werden ein eigenständig nutzbares
quadratisches Icon und eine horizontale Wort-Bild-Marke mit dem Projektnamen.
Die tatsächlichen Farben, Formen und Zuschnitte werden aus der gelieferten
Bildvorlage übernommen; aus dieser Beschreibung wird kein Ersatzlogo erzeugt.

## Vorhandene Dateien und Exportgrößen

Alle Pfade in dieser Tabelle sind relativ zu `doc/assets/branding/`.
`light` und `dark` bezeichnen den **Zielhintergrund**, nicht die Farbe des Logos.

| Datei | Verwendung | Größe |
|---|---|---|
| `admin-in-the-loop-source.png` | Unveränderte importierte Quell-PNG | 1672 × 941 px |
| `admin-in-the-loop-icon-light.png` | Eigenständiges Icon auf hellem Hintergrund | 512 × 512 px |
| `admin-in-the-loop-icon-dark.png` | Eigenständiges Icon auf dunklem Hintergrund | 512 × 512 px |
| `admin-in-the-loop-lockup-light.png` | Horizontale Wort-Bild-Marke auf hellem Hintergrund, mit dunkler Kontrastfläche | 880 × 290 px |
| `admin-in-the-loop-lockup-dark.png` | Horizontale Wort-Bild-Marke auf dunklem Hintergrund, transparent | 880 × 290 px |
| `admin-in-the-loop-lockup-ui-light.png` | UI-Größe für hellen nativen Oberflächenhintergrund | 220 × 73 px |
| `admin-in-the-loop-lockup-ui-dark.png` | UI-Größe für dunklen nativen Oberflächenhintergrund | 220 × 73 px |

Die Vorlage enthält bereits beide Formen auf transparentem Hintergrund. Das
Icon wurde aus `520x520+92+215` auf 512 × 512 px verkleinert; die Wort-Bild-Marke
wurde mit `880x290+744+382` in Originalauflösung ausgeschnitten. Die Koordinaten
sind ImageMagick-Geometrien mit Ursprung oben links in der Quell-PNG. Es wurde
nichts hochskaliert, nachgezeichnet, umgefärbt oder automatisch freigestellt.

Beide Icon-Dateien sind bytegleich: Das dunkle Terminalfeld mit heller Hand
funktioniert unverändert auf hellen und dunklen Hintergründen. Die weiße Schrift
der Wort-Bild-Marke benötigt auf hellem Hintergrund eine Kontrastfläche. Deshalb
ist die helle Lockup-Variante auf `#0b1117` zusammengesetzt; die dunkle Variante
behält die originale Transparenz. Die vorhandenen weichen Kanten und der
Schriftglanz der Vorlage bleiben erhalten. Für eine transparente Wortmarke auf
hellem Hintergrund wäre eine zusätzliche passende Vorlage nötig.

Die Exporte wurden mit ImageMagick erstellt, von Metadaten befreit und mit
OptiPNG verlustfrei komprimiert; die Quell-PNG blieb unverändert. Es gibt keine
verlustbehaftete Farbquantisierung. SVG-, Favicon- oder weitere Rastergrößen
sind für diese README-/Release-Vorbereitung nicht erforderlich. Die beiden
UI-Größen sind zusätzlich verlustfrei aus den jeweiligen Lockups mit Lanczos
auf 220 × 73 px verkleinert, damit Tk sie ohne externe Python-Bildbibliothek
pixelgenau anzeigen kann.

Quellname beim Import: `Admin_im_Terminal-Loop-Branding.png`.
SHA-256 der unveränderten Quelle:
`44a1f76fea3b78e9cd445ecc1df060e5b69c302c378f3d26c554a247c2cedbd5`.

## Einbindung im Root-README

Das Root-[README](../../../README.md) ist die Projekt-Landingpage. Die operative Anleitung
liegt unter `tests/Local_Integration_README.md`; die Produktreferenz liegt unter
`doc/Admin-in-the-Loop_Konzept_V0.7.md`. Deren interne Versionsangaben sind keine
v0.0.1-Releasekennzeichnung.

Im Root-README steht die horizontale Marke direkt unter der
Textüberschrift `# Admin in the Loop`, vor den bestehenden Leitsatz
**AI assists, Admin decides.** und die kurze Projektbeschreibung. Die
Textüberschrift bleibt für Lesbarkeit und Suche erhalten. Das Icon ist ein
separates Asset und wird nicht zusätzlich neben die vollständige Marke gesetzt.

Folgender Block ist im Root-README eingesetzt. Die Pfade gelten für das
Root-README, nicht für die Ablage dieses Asset-Guides:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="doc/assets/branding/admin-in-the-loop-lockup-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="doc/assets/branding/admin-in-the-loop-lockup-light.png">
  <img src="doc/assets/branding/admin-in-the-loop-lockup-light.png" alt="Admin in the Loop" width="600">
</picture>
```

Die helle Variante ist der Fallback. Beide Varianten wurden lokal auf hellem
und dunklem Hintergrund bei 600 px Lockup-Breite visuell geprüft.
Nach dem Einsetzen die Darstellung in
GitHub im hellen und dunklen Modus sowie die Lesbarkeit bei reduzierter Breite
prüfen. Bei geringerer Quellauflösung die Anzeigegröße entsprechend reduzieren.
Die Exporte sind über relative Repository-Pfade erreichbar und können aus der
v0.0.1-Releasebeschreibung verlinkt werden; diese Vorbereitung legt keine
zusätzlichen Release-Anhänge an.

In der Anwendungsoberfläche zeigt `ReviewUI` zwei Teile der Originalgrafik:
den grünen Terminal-Prompt links über „Terminal“ und die Leiterbahnen rechts
über „AI / MCP“. Der kompakte Texttitel bleibt im Hauptfenster-Header. Beide
Sektionsgrafiken (80×64 px) sind als Base64-Konstanten `_SECTION_LOGO_PNG`
direkt in `admin_in_the_loop.py` eingebettet und werden mit Tk
`PhotoImage(data=..., format="png")` geladen. Auch ein Download nur dieser
Python-Datei benötigt keine Assetordner, Dateiablage oder Netzwerkbeschaffung.

Ableitung aus der Original-PNG: Prompt `142x106+148+342`; Leiterbahnen
`172x194+390+360`, dabei den Handanschnitt links unten (`26x110+0+84` relativ
zum Zuschnitt) transparent aussparen. Proportional auf höchstens 68×54 bzw.
48×54 px verkleinern und auf 80×64 zentrieren. Keine neuen Formen zeichnen.
Die Light-Variante hat die bekannte Kontrastfläche `#0b1117`; die Dark-Variante
transparenten Außenrand. Die Auswahl folgt dem nativen `TFrame`-Hintergrund.
Bei Dekodierungsfehler bleiben die Abschnittstitel ohne Grafik erhalten.
Originaldateien und README-Lockups bleiben unverändert.

## Handzeichen auf den Review-Buttons

„Freigeben“ zeigt die OK-Hand (👌, U+1F44C); „Verwerfen“ die erhobene offene
Hand (✋, U+270B). Text, deaktivierte Zustände, Bestätigungen und Aktionen
bleiben erhalten. STOP bedeutet hier den Review verwerfen, nicht eine laufende
Shell oder Verbindung abbrechen. Der gemeinsame Footer richtet die Buttons
weiterhin auf derselben Höhe aus.

Die vorhandene Logo-Hand enthält keine OK-Geste. In der geprüften Tk/Xvfb-
Umgebung sind keine Fonts mit diesen beiden Emoji-Glyphen verfügbar. Deshalb
werden statt plattformabhängiger Text-Emojis transparente 24×24-PNGs direkt als
`_BUTTON_HAND_PNG` in der Python-Datei mitgeliefert. Beide Motive funktionieren
unverändert auf hellen/dunklen Buttonflächen und benötigen weder Fonts noch
zusätzliche Assets, Downloads oder Bildbibliotheken zur Laufzeit. Dekodiert Tk
ein Bild nicht, bleibt der betreffende Textbutton funktionsfähig.

Grafiken: **Twemoji v14.0.2, Twitter und Mitwirkende**, unter
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), ohne Gewähr.
Quellen: [OK-Hand, 1f44c.png](https://github.com/twitter/twemoji/blob/v14.0.2/assets/72x72/1f44c.png)
und [erhobene Hand, 270b.png](https://github.com/twitter/twemoji/blob/v14.0.2/assets/72x72/270b.png).
Änderung: proportional von 72×72 auf 24×24 verkleinert, Metadaten entfernt.
Diese Grafiklizenz gilt für die beiden Emoji-Bilder; Attribution und Lizenzlink
stehen auch in der einzeln herunterladbaren Python-Datei.

## Grenze dieser Vorbereitung

Import, Zuschnitt, Export und lokale visuelle Prüfung sind abgeschlossen.
Das Root-README ist erstellt; dessen tatsächliche GitHub-Darstellungsprüfung
steht nach der späteren Veröffentlichung noch aus. Die Logo-Einbindung in die
Anwendungsoberfläche ist implementiert; native Plattformdarstellung bleibt von
der jeweils unterstützten Tk-Oberfläche abhängig. Veröffentlichung, Tagging,
Release-Erstellung und Deployment sind nicht Teil dieser Vorbereitung.
