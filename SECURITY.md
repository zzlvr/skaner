# Security

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on this repository. Please do not open a public issue for a security problem. I answer within a few days.

## What is in scope

The engine (`docstruc/`) and the web app (`web/`): uploads, sessions, the provider key handling, exports, the admin area.

## How keys and documents are handled

- A provider key pasted on the upload page is kept in memory for that one upload. It is never stored, logged or shown.
- Uploaded files are not stored. Extraction results are stored encrypted, and guest results are deleted after a few days.
- Your documents go to the provider whose key you use, under your agreement with that provider.

If you find a case where any of this is not true, that is a vulnerability: please report it.
