# ign8mail documentation site

Built with [MkDocs](https://www.mkdocs.org/) + [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/).

## Local preview

```bash
cd docs
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdocs serve
```

Opens on <http://localhost:8000>. Edits to `docs/*.md` hot-reload.

## Build static site

```bash
mkdocs build      # output goes to docs/site/
```

## Deploy to GitHub Pages

```bash
mkdocs gh-deploy --force
```

## Layout

```
docs/
├── mkdocs.yml          # site config, nav, theme
├── requirements.txt    # mkdocs + material
└── docs/               # Markdown sources
    ├── index.md
    ├── quickstart.md
    ├── architecture.md
    ├── cli.md
    ├── dns.md
    ├── configuration.md
    └── operations.md
```
