"""Site configuration (CONTRACTS section 7) and secrets (section 9).

Usage:
    from common.config.site_config import load_site_config, load_secrets
    cfg = load_site_config()          # $DPC_SITE_CONFIG or ~/INSIGHTZZ/config/site.yaml
    secrets = load_secrets(cfg)       # <root>/config/secrets.env, must be chmod 600
"""
