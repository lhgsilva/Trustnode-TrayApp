# Aggregator package for optional, license-gated TrustNode modules.
# Each subpackage here is a self-contained feature module (router,
# service, models) that can be loaded or skipped at app startup
# based on the customer's license. Keeping them under a dedicated
# package makes it easy to grep for "modules/<feature>/..." and to
# disable a whole module by skipping its include_router call.
