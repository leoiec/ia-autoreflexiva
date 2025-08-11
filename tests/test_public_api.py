import modules.autonomous_agent as aa

def test_public_api_surface():
    for name in ["__version__", "make_agent", "run", "load_core", "enable_core",
                 "is_consent_given", "is_core_initialized", "is_core_enabled"]:
        assert hasattr(aa, name), f"Missing API: {name}"
