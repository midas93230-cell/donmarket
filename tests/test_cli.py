"""La ligne de commande — surtout ce qui la fait mourir à l'affichage.

Un rapport qui s'interrompt sur `UnicodeEncodeError` après vingt lignes ne
ressemble pas à un bug d'affichage : il ressemble à un scan qui a planté. Le
balayage dure de 48 à 71 s ; le perdre à l'écriture est le genre de panne qu'on
ne veut découvrir qu'une fois.
"""

from __future__ import annotations

import io

import pytest

from donmarket import cli


class TestForceUtf8Console:
    def test_survives_a_stream_without_reconfigure(self, monkeypatch):
        """`capsys` et les redirections remplacent les flux par des StringIO.

        Ils n'ont pas de `reconfigure`. Si l'absence levait, la CLI ne
        démarrerait plus sous test ni sous n'importe quelle redirection.
        """
        monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
        monkeypatch.setattr(cli.sys, "stderr", io.StringIO())

        cli.force_utf8_console()  # ne doit pas lever

    def test_survives_a_stream_that_refuses_to_reconfigure(self, monkeypatch):
        """Un flux détaché lève `ValueError` — ce n'est pas une raison d'échouer."""

        class Stubborn(io.StringIO):
            def reconfigure(self, **_: object) -> None:
                raise ValueError("underlying buffer has been detached")

        monkeypatch.setattr(cli.sys, "stdout", Stubborn())
        monkeypatch.setattr(cli.sys, "stderr", Stubborn())

        cli.force_utf8_console()  # ne doit pas lever

    def test_asks_for_utf8_and_replacement(self, monkeypatch):
        seen: list[dict[str, object]] = []

        class Recording(io.StringIO):
            def reconfigure(self, **kwargs: object) -> None:
                seen.append(kwargs)

        monkeypatch.setattr(cli.sys, "stdout", Recording())
        monkeypatch.setattr(cli.sys, "stderr", Recording())

        cli.force_utf8_console()

        assert seen == [
            {"encoding": "utf-8", "errors": "replace"},
            {"encoding": "utf-8", "errors": "replace"},
        ]


class TestSeparatorIsUnprintableInLatin1:
    def test_the_separator_is_exactly_what_broke(self):
        """Verrouille la cause : le séparateur n'est pas encodable en cp1252.

        Si quelqu'un le remplace un jour par des tirets ASCII, ce test tombe et
        rappelle que `force_utf8_console` protège aussi les accents et les
        flèches — pas seulement cette ligne-là.
        """
        with pytest.raises(UnicodeEncodeError):
            cli.SEPARATOR.encode("cp1252")


class TestParser:
    def test_history_has_sane_defaults(self):
        args = cli.build_parser().parse_args(["history"])

        assert args.min_observations == 2
        assert args.limit == 25

    def test_rewards_persists_unless_told_otherwise(self):
        parser = cli.build_parser()

        assert parser.parse_args(["rewards", "--bankroll", "100"]).no_persist is False
        assert (
            parser.parse_args(
                ["rewards", "--bankroll", "100", "--no-persist"]
            ).no_persist
            is True
        )
