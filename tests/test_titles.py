"""Normalización de títulos: consolidar ediciones sin fusionar discos distintos."""

from __future__ import annotations

import pytest

from spot_albums.titles import album_key, normalize, strip_edition, track_key


@pytest.mark.parametrize("entrada,esperado", [
    ("Kiss Me Kiss Me Kiss Me (Remastered 2006)", "Kiss Me Kiss Me Kiss Me"),
    ("In Rainbows - Deluxe Edition", "In Rainbows"),
    ("Nevermind (20th Anniversary Super Deluxe Edition)", "Nevermind"),
    ("Pornography [Deluxe Edition] (Remastered)", "Pornography"),
    ("OK Computer OKNOTOK 1997 2017", "OK Computer OKNOTOK 1997 2017"),
])
def test_recorta_sufijos_de_edicion(entrada, esperado):
    assert strip_edition(entrada) == esperado


@pytest.mark.parametrize("titulo", [
    "(What's the Story) Morning Glory?",
    "Sgt. Pepper's Lonely Hearts Club Band",
    "[Untitled]",
])
def test_no_toca_parentesis_que_son_parte_del_titulo(titulo):
    """Un falso positivo aquí borraría un álbum real del ranking."""
    assert strip_edition(titulo) == titulo


def test_las_ediciones_del_mismo_disco_colapsan():
    a = album_key("The Smiths", "The Queen Is Dead")
    b = album_key("The Smiths", "The Queen Is Dead (2011 Remaster)")
    c = album_key("the smiths", "The Queen Is Dead - Deluxe Edition")
    assert a == b == c


def test_discos_distintos_no_se_fusionan():
    assert album_key("Deftones", "White Pony") != album_key("Deftones", "Koi No Yokan")
    assert album_key("Weezer", "Weezer") != album_key("Weezer", "Pinkerton")
    # Mismo título, artistas distintos: no deben cruzarse.
    assert album_key("Weezer", "Weezer") != album_key("Nirvana", "Weezer")


def test_ignora_acentos_y_mayusculas():
    assert normalize("Corazón") == normalize("CORAZON")
    assert album_key("José José", "Reencuentro") == album_key("Jose Jose", "reencuentro")


@pytest.mark.parametrize("a,b", [
    ("Bigmouth Strikes Again", "Bigmouth Strikes Again - 2011 Remaster"),
    ("There Is a Light", "There Is a Light (Remastered)"),
    ("Just Like Heaven", "Just Like Heaven - 2006"),
])
def test_las_variantes_de_un_tema_cuentan_como_uno(a, b):
    """Si no, el breadth se pasa de 1.0 en discos muy reeditados."""
    assert track_key(a) == track_key(b)


def test_temas_distintos_siguen_distintos():
    assert track_key("A Rush of Blood to the Head") != track_key("The Scientist")


def test_titulo_vacio_no_revienta():
    assert normalize(None) == ""
    assert track_key(None) == ""
    assert album_key(None, None) == ("", "")
