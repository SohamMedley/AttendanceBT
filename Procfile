# Process definition for Render, Railway, Heroku and anything else that reads a
# Procfile. Render uses render.yaml; this keeps the other platforms working.
#
# One worker on purpose: the chain is loaded into memory and persisted by the
# app, so parallel processes would each hold their own copy of the ledger.
web: gunicorn "app:create_app()" --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 120
