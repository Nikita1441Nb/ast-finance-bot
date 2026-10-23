# Corporate Financial Assistant & Task Telegram Bot 🤖📈

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Anthropic Claude](https://img.shields.io/badge/AI-Anthropic%20Claude%20API-orange.svg)](https://docs.anthropic.com/)
[![PTB v21](https://img.shields.io/badge/Telegram-python--telegram--bot%20v21-2CA5E0.svg)](https://python-telegram-bot.org/)
[![Database](https://img.shields.io/badge/Database-SQLite%20(WAL)-lightgrey.svg)](https://sqlite.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An intelligent, production-ready corporate Telegram bot designed for financial accounting, automated invoice/receipt auditing, real-time balance analytics, and natural-language task scheduling. Powered by **Anthropic Claude API (Function Calling & Structured Outputs)** and built with a modular, asynchronous architecture.

---

## 🌟 Key Features

- **Multimodal Document Processing:**
  - Ingests invoices, receipts, and acts via photos (`JPEG`, `PNG`, `WEBP`) or files (`.xlsx`, `.xls`, `.pdf`, `.csv`, `.txt`).
  - Utilizes Claude's Vision and Tool Use to extract entities (amounts, dates, counterparties, companies, tax details) into validated schemas.
- **Human-in-the-Loop Verification (Drafting Pipeline):**
  - Staged draft confirmation workflow: preview parsed transactions, edit them interactively via chat before committing, or discard them.
- **Financial Analytics & Reporting:**
  - Automated balance calculations across internal legal entities.
  - Generates consolidated Excel reports (`openpyxl`) on-demand.
  - Interactive LLM financial analyst equipped with dynamic database context injection.
- **Natural-Language Reminders & Dispatching:**
  - Background asynchronous task scheduler.
  - Parses free-form scheduling requests (*"tomorrow at 3 PM"*, *"in 45 minutes"*, *"send now"*) via LLM function calling and delivers notifications to targeted employees.
- **Role-Based Access Control (RBAC):**
  - Granular permissions separating `Admin` and `Worker` views.
  - In-chat staff management with live updates.

---

## 🏗 Architecture Overview

```text
├── config.py           # Environment variables, logging, path management, and timezones
├── database.py         # SQLite connection manager (WAL mode, busy timeout), models, queries
├── parsers.py          # Document decoders (PDF, legacy & modern Excel) and Excel report generator
├── ai_services.py      # Claude API wrappers, function-calling tool schemas, and prompt engineering
├── main.py             # Telegram event handlers, interactive UI keyboards, and task scheduler
├── requirements.txt    # Project dependencies
├── .env.example        # Environment variable template
└── .gitignore          # Git exclusions for secrets, logs, and databases
