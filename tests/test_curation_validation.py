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
    assert c["trained"] is False
    assert c["status"]=="insufficient_reviews"
    assert c["reviewed"]==4
    assert c["accepted"]==2 and c["rejected"]==2
    assert c["weights"]==validation.SCORE_WEIGHTS["balanced"]
    assert c["factor_separation"]["flow"]>0
    assert c["factor_separation"]["diversity"]<0
    assert sum(c["weights"].values())==pytest.approx(1)
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
    old=validation.calibration("Mix","eclectic",current_identity_revision="old")
    assert old["reviewed"]==4 and old["trained"] is False
    assert old["status"]=="insufficient_reviews"


def test_feedback_uses_persistent_database_across_connections(tmp_path,monkeypatch):
    monkeypatch.setattr(validation,"FILE",tmp_path/"reviews.sqlite")
    validation.record("Soul",None,"100","accept","none","",True,"r","snap",None,{})
    assert validation.reviews("Soul")[0]["target_id"] is None
    with sqlite3.connect(validation.FILE) as con:
        assert con.execute("select count(*) from reviews").fetchone()[0]==1



def test_portfolio_includes_two_diverse_cases_per_genre():
    names=[
        "After Hours Jazz", "Live Jazz - Last Set & Ballads",
        "Ambient - Air, Light & Japanese Spaces",
        "Ambient - Low Light & Deep Listening",
        "Deep Funk & Soul","Evening - Deep Soul & Blues",
        "Evening - Jazz, Beats & Soul in the Same Room",
        "Evening - No Genre, No Apology",
    ]
    selected=validation.portfolio(names,per_category=2)
    assert len(selected["jazz"])==2
    assert len(selected["ambient"])==2
    assert len(selected["soul"])==2
    assert len(selected["mixed"])>=1
    all_names=sum(selected.values(),[])
    assert len(all_names)==len(set(all_names))
    assert all(name in names for name in all_names)


def test_validation_summary_reports_uncertainty_without_retroactive_success(
    tmp_path,monkeypatch
):
    monkeypatch.setattr(validation,"FILE",tmp_path/"review.sqlite")
    for i in range(4):
        validation.record("Jazz",str(i),str(i+10),
            "accept" if i<3 else "reject",
            "none" if i<3 else "mood",
            "",True,"rev","snap",.7,{"fit":.6})
    state=validation.summary(["Jazz"])["Jazz"]
    assert state["human_validated"] is False
    assert state["acceptance_rate"]==.75
    assert len(state["acceptance_interval_95"])==2
    assert state["acceptance_interval_95"][0]<.75<state["acceptance_interval_95"][1]
    assert state["distinct_targets"]==4



def test_stale_playlist_snapshots_are_excluded_from_learning_and_acceptance(
    tmp_path,monkeypatch
):
    monkeypatch.setattr(validation,"FILE",tmp_path/"reviews.sqlite")
    for i in range(8):
        validation.record("Jazz",str(i),str(i+10),
          "accept" if i<4 else "reject","none" if i<4 else "mood",
          "",True,"same_identity","old_order",.8,
          {"fit":.8 if i<4 else .3,"flow":.7 if i<4 else .4})
    old=validation.summary(["Jazz"],{"Jazz":("old_order","same_identity")})["Jazz"]
    assert old["reviewed"]==8
    current=validation.summary(["Jazz"],{"Jazz":("new_order","same_identity")})["Jazz"]
    assert current["reviewed"]==0
    assert current["reviewed_historical"]==8
    assert not current["human_validated"]
    calibration=validation.calibration("Jazz","balanced",
       current_identity_revision="same_identity",
       current_playlist_snapshot="new_order")
    assert calibration["reviewed"]==0
    assert calibration["trained"] is False
    assert validation.feedback_for("Jazz","same_identity","new_order")=={}
    assert len(validation.feedback_for("Jazz","same_identity","old_order"))==8
