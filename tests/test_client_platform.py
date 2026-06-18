from pysaka import Group
from pysaka.client import GROUP_CONFIG


def test_group_config_has_verified_mobile_hosts():
    assert GROUP_CONFIG[Group.NOGIZAKA46]["mobile_api_base"] == "https://api.n46.glastonr.net/v2"
    assert GROUP_CONFIG[Group.HINATAZAKA46]["mobile_api_base"] == "https://api.kh.glastonr.net/v2"
    assert GROUP_CONFIG[Group.SAKURAZAKA46]["mobile_api_base"] == "https://api.s46.glastonr.net/v2"
    # Yodel has no known mobile host
    assert GROUP_CONFIG[Group.YODEL]["mobile_api_base"] is None
