#!/usr/bin/env bash
# Força um sync agora, sem esperar os 30 min do cron.
#
#   ./rodar.sh          → processa o que chegou e manda o painel
#   ./rodar.sh rapido   → só processa, sem mandar o painel
set -euo pipefail

REPO="igormichalski/financas"
RELATORIO="true"
[ "${1:-}" = "rapido" ] && RELATORIO="false"

gh workflow run sync.yml --repo "$REPO" -f relatorio="$RELATORIO"
echo "Disparado. Aguardando..."

sleep 5
ID=$(gh run list --repo "$REPO" --workflow sync.yml --limit 1 --json databaseId --jq '.[0].databaseId')

until [ "$(gh run view "$ID" --repo "$REPO" --json status --jq .status)" = "completed" ]; do
  sleep 5
done

CONCLUSAO=$(gh run view "$ID" --repo "$REPO" --json conclusion --jq .conclusion)
if [ "$CONCLUSAO" = "success" ]; then
  echo "✅ Pronto — confere o Telegram."
else
  echo "❌ Falhou ($CONCLUSAO). Log:"
  gh run view "$ID" --repo "$REPO" --log-failed | tail -30
  exit 1
fi
