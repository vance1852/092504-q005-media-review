"""定义基础服务允许登记的资料类别与评审业务常量。"""

ALLOWED_CATEGORIES = frozenset({
    "institution_profile",
    "venue_registry",
    "resource_registry",
    "participant_assignment",
})

# 作品后台允许的操作者角色。
ROLES = frozenset({"admin", "operator", "reviewer", "auditor", "author"})

# 素材许可声明取值。
LICENSE_STATUSES = frozenset({"owned", "cc0", "cc_by", "licensed", "permission", "unknown"})

# 必须随包提供版权凭据摘要的许可类型。
CREDENTIAL_REQUIRED_LICENSES = frozenset({"licensed", "permission"})

# 交付清单中必须确认的条目。
REQUIRED_CHECKLIST_ITEMS = (
    "script",
    "material_manifest",
    "interaction_notes",
    "author_declaration",
    "evidence_package",
)


def is_allowed_category(value: str) -> bool:
    return value in ALLOWED_CATEGORIES
