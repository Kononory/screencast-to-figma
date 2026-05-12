# Screencast → Figma

Drop a mobile screen recording. Get labeled, grouped screens imported directly into Figma.

Extracts unique frames from MP4/MOV, classifies them with AI (onboarding, paywall, home, settings, etc.), and lays them out in Figma sections with component tags and interaction states.

---

## No Figma Community needed

The plugin loads directly from this repo — no review, no waiting, works on any device.

**1. Clone the repo and run the installer**

```bash
git clone https://github.com/Kononory/screencast-to-figma.git
cd screencast-to-figma
```

```bash
# macOS / Linux
bash install.sh

# Windows — double-click install.bat
```

The script checks your Python version, creates a virtual environment, and installs all dependencies. It also warns if ffmpeg is missing.

**Start the server** (every time you use it):

```bash
# macOS / Linux
source venv/bin/activate && python app.py

# Windows
venv\Scripts\activate && python app.py
```

**2. Load the plugin in Figma desktop**

> Main menu → Plugins → Development → **Import plugin from manifest**
> → navigate to the cloned repo and select `static/figma-plugin/manifest.json`

**3. Open the plugin, paste your API key, drop a video**

On first launch the plugin asks which AI provider to use (Gemini, OpenAI, or Claude) and your API key. That's it.

---

## Requirements

- Python 3.10+
- Figma desktop app
- ffmpeg on PATH:

  ```bash
  # macOS
  brew install ffmpeg

  # Ubuntu / Debian
  sudo apt install ffmpeg

  # Windows — download from https://ffmpeg.org/download.html and add to PATH
  ```

- API key from one of:
  - [Google AI Studio](https://aistudio.google.com/apikey) — Gemini (free tier)
  - [OpenAI](https://platform.openai.com/api-keys)
  - [Anthropic](https://console.anthropic.com/)
  - Or skip AI entirely — plugin still extracts and imports frames

---

## What you get in Figma

- Screens grouped into labeled sections (Onboarding, Paywall, Home, Settings…)
- Key text under each frame (headline, price, active tab)
- Component tags in purple (price_cards, feature_checklist, bottom_tab_bar…)
- Interaction state in orange when non-default (keyboard_open, item_selected…)
- UX analysis block to the left of all sections (monetization hypothesis, onboarding flow, A/B bets)
