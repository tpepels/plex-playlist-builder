"""Step 4: actual-playlist selection, human listening feedback and bounded learning."""
import sqlite3

import pytest

from app import curation_validation as validation


def test_selects_four_distinct_real_style_playlist_names():
    names=[
      "Live Jazz - Last Set & Ballads",
      "Ambient - Low Light & Deep Listening",
      "Deep Funk & Soul",
      "Evening - Jazz, Beats & Soul in the Same Room",
      "All Music",
    ]
    selected=validation.categories(names)
    assert selected["jazz"]=="Live Jazz - Last Set & Ballads"
    assert selected["ambient"]=="Ambient - Low Light & Deep Listening"
    assert selected["soul"]=="Deep Funk & Soul"
    assert selected["mixed"]=="Evening - Jazz, Beats & Soul in the Same Room"
    assert len(set(selected.values()))==4


def test_incomplete_musical_families_are_explicitly_missing():
    selected=validation.categories(["Ambient Only"])
    assert selected["ambient"]=="Ambient Only"
    assert selected["jazz"] is None
    assert selected["mixed"] is None
    assert selected["soul"] is None


def test_listening_confirmation_and_rejection_reason_are_required(tmp_path,monkeypatch):
    monkeypatch.setattr(validation,"FILE",tmp_path/"reviews.sqlite")
    with pytest.raises(ValueError,match="Listening"):
        validation.record("Jazz","1","7","accept","none","",False,"rev","snapshot",.7,
                          {"fit":.8})
    with pytest.raises(ValueError,match="why"):
        validation.record("Jazz","1","7","reject","none","",True,"rev","snapshot",.7,
                          {"fit":.8})
    assert validation.reviews("Jazz")==[]


def test_review_history_and_conservative_weight_calibration(tmp_path,monkeypatch):
    monkeypatch.setattr(validation,"FILE",tmp_path/"reviews.sqlite")
    no=validation.calibration("Jazz","balanced",current_identity_revision="rev")
    assert no["trained"] is False
    assert no["weights"]==validation.SCORE_WEIGHTS["balanced"]
    records=[
        ("1","7","accept","none", {"fit":.9,"diversity":.5,"flow":.85,"intent":.9}),
        ("2","8","accept","none", {"fit":.92,"diversity":.6,"flow":.82,"intent":.91}),
        ("3","9","reject","flow", {"fit":.9,"diversity":.8,"flow":.1,"intent":.3}),
        ("4","10","reject","mood", {"fit":.91,"diversity":.9,"flow":.2,"intent":.2}),
    ]
    for target,candidate,verdict,issue,factors in records:
        validation.record("Jazz",target,candidate,verdict,issue,"Compared by ear",True,
                           "rev","snap",.8,factors)
    c=validation.calibration("Jazz","balanced",current_identity_revision="rev")
    assert c["trained"] is True
    assert c["reviewed"]==4
    assert c["accepted"]==2 and c["rejected"]==2
    assert c["weights"]["flow"]>validation.SCORE_WEIGHTS["balanced"]["flow"]
    assert c["weights"]["diversity"]<validation.SCORE_WEIGHTS["balanced"]["diversity"]
    assert sum(c["weights"].values())==pytest.approx(1)
    assert max(c["weights"].values())<.45
    assert validation.feedback_for("Jazz","rev")[("1","7")]["verdict"]=="accept"
    assert validation.feedback_for("Jazz","other")=={}
    # Re-submission revises an existing judgment, no double-counting.
    validation.record("Jazz","1","7","reject","metadata","Changed opinion",True,
                       "rev","snap",.8,{"fit":.2})
    assert len(validation.reviews("Jazz"))==4
    state=validation.summary(["Jazz","Ambient"])
    assert state["Jazz"]["reviewed"]==4
    assert state["Jazz"]["rejected"]==3
    assert state["Ambient"]["human_validated"] is False


def test_stale_identity_feedback_does_not_retrain_new_preferences(tmp_path,monkeypatch):
    monkeypatch.setattr(validation,"FILE",tmp_path/"reviews.sqlite")
    for i in range(4):
        validation.record("Mix",str(i),str(i+10),"accept" if i<2 else "reject",
            "none" if i<2 else "mood","",True,"old","snap",.7,
            {"intent":.9 if i<2 else .1})
    assert validation.calibration("Mix","eclectic",
           current_identity_revision="new")["trained"] is False
    assert validation.calibration("Mix","eclectic",
           current_identity_revision="old")["trained"] is True


def test_feedback_uses_persistent_database_across_connections(tmp_path,monkeypatch):
    monkeypatch.setattr(validation,"FILE",tmp_path/"reviews.sqlite")
    validation.record("Soul",None,"100","accept","none","",True,"r","snap",None,{})
    assert validation.reviews("Soul")[0]["target_id"] is None
    with sqlite3.connect(validation.FILE) as con:
        assert con.execute("select count(*) from reviews").fetchone()[0]==1
