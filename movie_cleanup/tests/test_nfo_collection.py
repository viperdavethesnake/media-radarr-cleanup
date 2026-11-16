import xml.etree.ElementTree as ET

from movie_cleanup.batch_cleaner import write_nfo


def _meta(collection):
    return {"id": 238, "title": "The Godfather", "release_date": "1972-03-14",
            "belongs_to_collection": collection}


def test_collection_id_written_where_jellyfin_reads_it(tmp_path):
    # Jellyfin ignores <setid>; it reads <collectionnumber> into TmdbCollection.
    dest = tmp_path / "movie.nfo"
    write_nfo(_meta({"id": 230, "name": "The Godfather Collection"}), "tt0068646", str(dest))
    root = ET.parse(dest).getroot()
    assert root.findtext("collectionnumber") == "230"
    assert root.findtext("setid") == "230"


def test_no_collectionnumber_for_standalone_movie(tmp_path):
    dest = tmp_path / "movie.nfo"
    write_nfo(_meta(None), "tt0068646", str(dest))
    assert ET.parse(dest).getroot().find("collectionnumber") is None
