from unidecompiler.plugins import FrontendVersionSupport

VERSION_SUPPORT = FrontendVersionSupport(
    family="__VM_NAME__",
    versions=__VERSIONS__,
    parser="replace-with-parser-name",
    status="experimental",
)
