#!/bin/bash
set -e

# Install/sync Python dependencies
pip install -r requirements.txt --quiet

# The app runs its own DB migrations at startup (app.py),
# so no separate migration command is needed here.
echo "Post-merge setup complete."
