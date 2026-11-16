# ZivoIT Retail LMS AI — Odoo 19

AI-enabled Learning Management System for multi-store retailers in any sector (fashion, footwear,
electronics, beauty, grocery, home, jewellery, pharmacy and more).

**Module:** `zivoit_retail_lms` · **Version:** 19.0.1.1.0 · **Odoo:** 19.0 Community & Enterprise · **License:** OPL-1 · **Price:** USD 659
**Publisher:** Zivo IT Technologies (Shrisampadha Private Limited) · www.zivoit.com · info@zivoit.com

---

## Module contents

```
zivoit_retail_lms/
├── README.md                     ← this guide (install, setup, AI, demo, App Store)
├── __manifest__.py               ← USD 659 · OPL-1 · Odoo 19
├── doc/
│   ├── ZivoIT_Retail_LMS_AI_Functional_Specification.pdf  ← branded functional specification
│   ├── FEATURES.md               ← full feature reference
│   └── CHANGELOG.md              ← release notes
├── static/description/           ← App Store cover (banner.png), icon, listing (index.html),
│                                    ZIVO IT logo and 22 screenshots
├── models/ views/ wizards/ controllers/ security/ data/ report/
└── tests/                        ← 36 automated tests
```

## 1. Install

1. Copy this `zivoit_retail_lms` folder into your Odoo 19 custom addons directory.
2. Restart the Odoo service.
3. Turn on Developer Mode → **Apps → Update Apps List**.
4. Search **"Retail LMS"** → **Activate**.

Odoo installs the dependencies automatically: eLearning, Surveys, Employees, Portal, Product and Discuss.
No extra Python libraries are needed.

Command-line alternative:

```bash
./odoo-bin -c odoo.conf -d <database> -i zivoit_retail_lms
```

## 2. First-time setup (about 15 minutes)

| Step | Where | What to do |
|---|---|---|
| 1 | Retail LMS › Configuration › Settings | Set **Brand Context** (brand name, retail sector, market). Optionally connect an **AI provider** and click *Test Connection*. |
| 2 | Configuration › Regions / Roles & Career Ladder | Review regions, roles, skill targets and the next role for each role. |
| 3 | Network › Stores | Create stores and assign a **Store Manager** user to each. |
| 4 | Users | Give store managers the *Retail LMS › Store Manager* group and the L&D team *L&D Administrator*. |
| 5 | Content › Product Knowledge | Add product cards, then **AI: Build Micro-course**. |
| 6 | Content › Learning Paths (or AI Studio › AI Learning Path Builder) | Build journeys, tick *Auto-assign* for onboarding, then **Publish**. |
| 7 | Network › Learners | Add staff (or Employees › Action › *Create Retail Learner Profiles*), then **Grant Learning Access**. |
| 8 | Staff phones | Staff open `https://<your-odoo>/my/learning` and sign in. Portal users don't need a paid Odoo licence. |

## 3. AI providers

Settings › Retail LMS › AI Provider:

| Provider | What to enter |
|---|---|
| OpenAI | API key (default model `gpt-4.1-mini`) |
| Anthropic Claude | API key (default model `claude-sonnet-5-5`) |
| Google Gemini | API key (default model `gemini-2.5-flash`) |
| OpenAI-compatible (Azure, Groq, Ollama, Sarvam...) | Base URL plus key/model |

With no provider set, the built-in **offline engine** keeps every feature working. Every AI call is
logged under Configuration › AI Usage Log.

## 4. Try the demo

Install on a database **with demo data**. You get a fictional multi-category retailer: 6 stores,
14 learners, 5 product cards, 4 courses, 3 journeys, 4 role-play scenarios, classroom sessions,
bulletins, coaching observations, badges and 6 months of store KPIs.

| Login | Password | Sees |
|---|---|---|
| `admin` | `admin` | Everything (L&D Administrator) |
| `demo` | `demo` | Store Manager of 2 stores |
| `portal` | `portal` | Learner "Joel Willis" — open `/my/learning` on a phone |

## 5. Scheduled jobs (created automatically)

Hourly progress sync · daily overdue reminders · certificate expiry & re-certification ·
Daily Boost card seeding · badge awards · overdue bulletin reminders.

## 6. Quality

36 automated tests (business flows, security, portal, mocked AI providers, headless-browser UI checks):

```bash
./odoo-bin -c odoo.conf -d <test_db> -i zivoit_retail_lms --test-tags /zivoit_retail_lms --stop-after-init
```

See `doc/FEATURES.md` for the full feature reference and `doc/ZivoIT_Retail_LMS_AI_Functional_Specification.pdf` for the functional specification.

## 7. Publish on the Odoo App Store

1. Put this `zivoit_retail_lms` folder at the root of a Git repository, on a branch named `19.0`.
2. Sign in at apps.odoo.com › **Upload** and register the repository URL and branch.
3. The App Store reads the price (USD 659), cover (`static/description/banner.png`), icon and listing (`static/description/index.html`) from the module.
