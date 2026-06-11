"""Validate the predicted-lineup core: formation->roles, manual seed dict, and
the HTML parser against a mock predicted-XI page. No network needed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datalayer.lineups import (  # noqa: E402
    formation_to_roles,
    build_predicted_xi,
    ManualLineups,
    HtmlPredictedLineups,
)


def test_formation_known():
    roles = formation_to_roles("4-2-3-1")
    assert len(roles) == 11
    assert roles[0] == "GK"
    assert roles[-1] == "ST"
    assert roles.count("CB") == 2 and roles.count("FB") == 2
    print("known formation ok:", roles)


def test_formation_fallback():
    roles = formation_to_roles("4-4-1-1")  # not in table -> generic
    assert len(roles) == 11 and roles[0] == "GK"
    print("fallback formation ok:", roles)


def test_build_xi_doubtful():
    xi = build_predicted_xi(
        "4-3-3",
        ["Alisson", "Danilo", "Marquinhos", "Gabriel", "Wendell",
         "Casemiro", "Bruno", "Raphinha", "Rodrygo", "Richarlison", "Vinicius"],
        doubtful=["Rodrygo"],
    )
    assert len(xi) == 11
    assert xi[0] == {"name": "Alisson", "pos": "GK", "startProb": 0.85}
    rod = next(p for p in xi if p["name"] == "Rodrygo")
    assert rod["startProb"] == 0.55  # doubtful -> discounted
    print("build XI ok; doubtful Rodrygo startProb =", rod["startProb"], "pos =", rod["pos"])


def test_manual_seed_dict():
    m = ManualLineups({
        "Brazil": {"formation": "4-2-3-1",
                   "starters": ["Alisson", "Danilo", "Marquinhos", "Gabriel", "Wendell",
                                "Casemiro", "Bruno", "Raphinha", "Paqueta", "Vinicius", "Richarlison"],
                   "doubtful": []},
    })
    seed = m.to_seed_dict()
    assert "Brazil" in seed and len(seed["Brazil"]) == 11
    # the lone striker slot in 4-2-3-1
    assert seed["Brazil"][-1]["pos"] == "ST"
    print("manual seed dict ok; striker =", seed["Brazil"][-1])


MOCK_HTML = """
<html><body>
  <div class="predicted-lineup">
    <span class="team-name">Brazil</span>
    <span class="formation">4-3-3</span>
    <ul>
      <li class="player"><span class="name">Alisson</span></li>
      <li class="player"><span class="name">Danilo</span></li>
      <li class="player"><span class="name">Marquinhos</span></li>
      <li class="player"><span class="name">Gabriel</span></li>
      <li class="player"><span class="name">Wendell</span></li>
      <li class="player"><span class="name">Casemiro</span></li>
      <li class="player"><span class="name">Guimaraes</span></li>
      <li class="player doubtful"><span class="name">Paqueta</span></li>
      <li class="player"><span class="name">Raphinha</span></li>
      <li class="player"><span class="name">Richarlison</span></li>
      <li class="player"><span class="name">Vinicius</span></li>
    </ul>
  </div>
</body></html>
"""


def test_html_parse():
    try:
        import bs4  # noqa: F401
    except ImportError:
        print("html parse SKIPPED (install beautifulsoup4 to run)")
        return
    seed = HtmlPredictedLineups("http://example", selectors={}).parse(MOCK_HTML)
    assert "Brazil" in seed and len(seed["Brazil"]) == 11
    paqueta = next(p for p in seed["Brazil"] if p["name"] == "Paqueta")
    assert paqueta["startProb"] == 0.55  # doubtful class picked up
    print("html parse ok; doubtful Paqueta startProb =", paqueta["startProb"])


if __name__ == "__main__":
    test_formation_known()
    test_formation_fallback()
    test_build_xi_doubtful()
    test_manual_seed_dict()
    test_html_parse()
    print("\nALL LINEUP TESTS PASSED")
