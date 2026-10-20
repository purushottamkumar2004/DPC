"""Site configuration and model packages.

    from common.config.site_config import load_site_config      # site.yaml v3 (CONTRACTS 7.1, 9)
    from common.config.model_package import load_model_package  # model packages (CONTRACTS 12)

    cfg = load_site_config()          # $DPC_SITE_CONFIG or ~/INSIGHTZZ/config/site.yaml; must be chmod 600
    package = load_model_package(cfg) # verified weights, Detectron yaml, classes and label map
"""
