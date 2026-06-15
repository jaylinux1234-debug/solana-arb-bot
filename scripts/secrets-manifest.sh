# Shared secret file list (sourced by setup-secrets.sh / encrypt / decrypt).
# .txt files are editor-friendly; Docker mounts use the same basename without requiring .txt.

SECRET_FILES=(
  private_key
  private_key.txt
  private_key_cex_dex
  jupiter_api_key
  openai_api_key
  backpack_secret
  helius_api_key
  oneinch_api_key.txt
  cow_api_key.txt
  pagerduty_routing_key.txt
)
