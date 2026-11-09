# Docmost To Claude Project Converter

[![Build](https://img.shields.io/github/actions/workflow/status/tomdewildt/docmost-to-claude-project-converter/ci.yml?branch=master)](https://github.com/tomdewildt/docmost-to-claude-project-converter/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/tomdewildt/docmost-to-claude-project-converter)](https://github.com/tomdewildt/docmost-to-claude-project-converter/blob/master/LICENSE)

Convert [Docmost](https://docmost.com/) pages to [Claude](https://claude.ai/) project knowledge, using the Docmost export API and the Claude web app through [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).

# How To Run

Prerequisites:

- mise version `2025.1.0` or later
- uv version `0.6.0` or later
- python version `3.13.0` or later
- docker version `27.0.0` or later
- google chrome version `130.0.0` or later

### Development

1. Run `mise run init` to initialize the environment.
2. Copy `.env.example` to `.env` and configure the settings. Set either `CONV_DOCMOST_API_KEY` or `CONV_DOCMOST_EMAIL` and `CONV_DOCMOST_PASSWORD`.
3. Run `mise run start -- login` to sign in to Claude.
4. Run `mise run start -- pull <page>` to export a page and its subpages to `data/output`.
5. Run `mise run start -- push <project>` to upload `data/output` to a Claude project.
6. Run `mise run start -- sync <page> <project>` to pull, push and remove all other project knowledge.
7. Run `mise run test` to run all tests.

Run `mise run docmost:start` to start the docmost server and `mise run docmost:stop` to stop it.

# References

[Docmost Docs](https://docmost.com/docs/)

[Patchright Python Docs](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)

[Playwright Python Docs](https://playwright.dev/python/)

[HTTPX Docs](https://www.python-httpx.org/)

[Pydantic Settings Docs](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)

[Loguru Docs](https://loguru.readthedocs.io/)

[Docker Compose Docs](https://docs.docker.com/compose/)
