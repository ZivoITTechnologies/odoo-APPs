# ZivoIT Retail LMS AI (Odoo 19)

AI-enabled Learning Management System for any multi-store retailer - fashion, footwear, electronics,
beauty, grocery, home, jewellery, pharmacy and more - across company-owned stores, franchises,
shop-in-shops and kiosks. Built on top of Odoo eLearning, Survey and HR.

Technical name: `zivoit_retail_lms` · Version 19.0.1.0.0 · Works on Community and Enterprise.

## What it covers

| Retail requirement | How the module delivers it |
|---|---|
| Structured onboarding & employee training | Role-based **Learning Paths** (30/60/90-day journeys) mixing courses, certification exams, AI role-plays and on-the-job (OJT) tasks. Auto-assigned the moment a learner joins a store or changes role. |
| Product knowledge & technology learning | **Product Knowledge Cards** (Feature · Advantage · Benefit, features & technologies, objections, cross-sell). The **AI Course Builder** turns cards or any SOP text into a micro-lesson + quiz in 17 languages (English, Spanish, French, German, Portuguese, Arabic, Indonesian, Chinese, Hindi, Tamil, Telugu and more). |
| Customer service & selling skills | **AI Role-play Coach**: an AI customer (price objection, complaint, cross-sell, discovery...) chats with the associate, then scores a rubric and suggests a better line. |
| Assessments, quizzes, certifications | Native Odoo quizzes and Survey certifications, first-attempt scoring, pass marks, numbered PDF certificates with expiry, automatic re-certification. |
| Progress & performance tracking | Live progress engine synchronised from eLearning/Survey/role-plays, overdue tracking, readiness score, at-risk detection, AI skill-gap analysis per learner. |
| Centralised dashboards & reporting | OWL dashboard (network → region → store), store league table, path completion, leaderboard, at-risk learners, pivots/graphs, **Training Impact** report linking training completion to conversion, ATV, UPT, CSAT. |
| Mobile-friendly learning | Mobile-first learner portal `/my/learning`: journeys, **Daily Boost** spaced-repetition quiz, role-play chat, Ask the Expert, certificate download. Learners can be **portal users** (no internal licence needed). |

## Engagement, coaching & career

| Feature | What it does |
|---|---|
| **Store Bulletins** | Launch briefs, promotions, policy/SOP changes, planograms and recognition - targeted by region, store and role, with attachments, a linked micro-course, read & acknowledge tracking, overdue reminders to store managers. |
| **Badges & streaks** | 11 ready-made badges (points, journeys, certificates, Daily Boost streak/mastery, role-play score, bulletins, observations, classroom attendance) with bonus points; fully configurable. |
| **Store Challenge** | Monthly league of stores by learning points per head - fair across store sizes - on the dashboard and every learner's phone. |
| **Skills Matrix** | Role skill targets; learner skill levels built from coaching observations, AI role-plays and manager ratings; gap analysis and pivot matrix. |
| **Career Ladder** | Next role, required journeys and minimum tenure per role; promotion-readiness score and "Ready for promotion" list. |
| **Coaching Observations** | Floor-walk checklists for store managers, 1-5 ratings per behaviour mapped to skills, AI coaching plan (praise, focus areas, drills, follow-up). |
| **Instructor-led Training** | Classroom, virtual and on-the-floor sessions with calendar, capacity, auto-invite of learners who need it, attendance, learner feedback; counts as a journey step. |
| **AI Learning Path Builder** | Describe the goal; get a sequenced draft journey from your courses, role-plays, classroom courses and on-the-job tasks. |

## AI

Settings → Retail LMS → **Brand Context**: brand name, retail sector (13 sectors) and market. Every AI
prompt is grounded in this, so the same module serves a fashion chain, an electronics retailer or a pharmacy.

Settings → Retail LMS → AI Provider: **OpenAI**, **Anthropic Claude**, **Google Gemini** or any
**OpenAI-compatible** endpoint (Azure OpenAI, Groq, Ollama, Sarvam...). Every call is logged
(Configuration → AI Usage Log).

Without a key, a built-in **offline engine** keeps every feature working: FAB lessons and quizzes from
product cards, cloze questions from SOP text, scripted role-play customers with keyword rubric scoring,
rule-based skill-gap and store-health analysis, keyword retrieval for Ask the Expert.

## Roles & security

* **L&D Administrator** – everything, AI studio, settings.
* **Store Manager** – sees only the stores they manage (or their area, as area manager); signs off OJT tasks.
* **Learners** – portal (or internal) users; access only their own data through `/my/learning`.

## Scheduled jobs

Hourly progress sync · daily overdue reminders (email + manager activity) · daily certificate expiry and
re-certification · daily Daily Boost card seeding.

## Getting started

1. Install `zivoit_retail_lms` (pulls in eLearning, Survey, HR, Portal).
2. Configuration → Regions, Retail Roles; Network → Stores (set store managers).
3. Content → Product Knowledge → **AI: Build Micro-course**.
4. Content → Learning Paths → add steps → **Publish** (tick *Auto-assign* for onboarding).
5. Network → Learners (or *Create Retail Learner Profiles* from Employees) → **Grant Learning Access**.
6. Learners open `/my/learning` on their phones.

Install with demo data to see a fictional multi-category retailer: 6 stores, 14 learners, apparel, footwear, electronics, kitchen and beauty products, 3 journeys, role-plays, KPIs and a populated dashboard.

## Tests

36 automated tests (`--test-tags /zivoit_retail_lms`): journeys, progress sync, certificates,
re-certification, Leitner algorithm, KPIs, dashboard, wizard, record rules, portal flows, mocked
OpenAI/Claude/Gemini payloads and AI fallback, bulletins, badges, streaks, store challenge, observations, skills, promotion readiness, classroom sessions, path builder, plus headless-browser checks of the dashboard and views.

---
Zivo IT Technologies (Shrisampadha Private Limited) · www.zivoit.com · info@zivoit.com
