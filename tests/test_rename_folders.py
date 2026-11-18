"""After the VinylFlow -> FlipsideFlow rename, an existing install keeps its folders."""
import desktop_launcher as launcher


def test_existing_vinylflow_folder_is_kept(tmp_path):
    legacy, new = tmp_path / "VinylFlow", tmp_path / "FlipsideFlow"
    legacy.mkdir()
    assert launcher._prefer_existing(new, legacy) == legacy


def test_new_installs_and_migrated_ones_use_flipsideflow(tmp_path):
    legacy, new = tmp_path / "VinylFlow", tmp_path / "FlipsideFlow"
    assert launcher._prefer_existing(new, legacy) == new      # nothing yet
    legacy.mkdir()
    new.mkdir()
    assert launcher._prefer_existing(new, legacy) == new      # both: the new name wins


def test_app_name():
    assert launcher.APP_NAME == "FlipsideFlow" and launcher.LEGACY_APP_NAME == "VinylFlow"
