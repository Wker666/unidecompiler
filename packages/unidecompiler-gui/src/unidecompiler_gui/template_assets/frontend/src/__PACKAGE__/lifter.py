from unidecompiler.plugins import FrontendModule


def lift_module(module: FrontendModule):
    """Submit complete VMBytecodeStep streams to core from this module's model."""
    raise NotImplementedError("thin-IR lifting has not been implemented")
