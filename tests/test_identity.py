"""Identity links a fact to its articles: by the keys its record carries, by its subject exactly, and by a near match
a judge settles."""

from __future__ import annotations

from cerebro.article import Identity
from cerebro.connector import Record
from cerebro.facts import Fact, Mention
from cerebro.fakes import FakeDecider, MemoryStore
from cerebro.identity import DecisionJudge, Judged, link, link_ties, link_to, ties
from cerebro.models import TokenCount


def fact(subject: str) -> Fact:
    return Fact(id=f"f_{subject or 'none'}", text="x", subject=subject, kind="note", at="", source="mail",
                record="mail:1")


def test_a_fact_links_by_its_records_keys_and_by_its_exact_subject_and_never_to_a_shared_name_unasked() -> None:
    store = MemoryStore()
    store.create(Identity("people/ada", "people", "Ada Park", {"email": ["ada@example.com"]}))
    store.create(Identity("teams/apollo", "teams", "Apollo"))
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    store.create(Identity("projects/vega", "projects", "Vega"))
    mail = Record(id="mail:1", source="mail", revision="1", keys={"email": ["ADA@example.com"]})
    assert link(store, fact("vega"), mail) == ["people/ada", "projects/vega"]
    assert link(store, fact("Apollo"), None) == []
    assert link(store, fact("Vega launch"), None) == []
    assert store.facts_of("projects/vega") == ["f_vega"] and store.facts_of("people/ada") == ["f_vega"]

    asked = FakeDecider()
    assert link(store, fact("Apollo"), None, DecisionJudge(asked)) == [] and asked.asked == []
    named = fact("")
    named.mentions = [Mention("Ada", "speaker", {"email": ["ada@example.com"]}), Mention("Vega", "about")]
    assert link(store, named, None) == ["people/ada", "projects/vega"]


def test_a_record_that_opens_with_a_line_naming_an_article_links_its_facts() -> None:
    store = MemoryStore()
    store.create(Identity("parts/alder", "parts", "Alder Works"))
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    note = Record(id="notes/1.md", source="local", revision="1",
                  keys={"account": ["Alder Works"], "projects": ["Apollo, Nobody Here"]})
    assert link(store, fact(""), note) == ["parts/alder"]  # a line of several names links only what names one
    assert link(store, fact("Apollo"), note) == ["projects/apollo", "parts/alder"]


def test_a_near_match_is_judged_only_when_a_judge_is_given_and_links_when_one_candidate_passes() -> None:
    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder Works"))
    store.create(Identity("things/alder-co", "things", "Alder"))
    store.create(Identity("projects/vega", "projects", "Vega"))
    near = fact("Alder Works's security review")
    assert link(store, near) == []
    decider = FakeDecider({"things/alder": 0.9, "things/alder-co": 0.2})
    spent: list[Judged] = []
    assert link_ties(store, near.id, ties(store, near, judge=DecisionJudge(decider), spent=spent)) == ["things/alder"]
    state, questions = decider.asked[0]
    assert sorted(questions) == ["things/alder", "things/alder-co"] and state["fact"] == "x"
    assert spent == [Judged(["things/alder"], TokenCount(), "fake")]

    both = FakeDecider({"things/alder": 0.9, "things/alder-co": 0.8})
    assert ties(store, fact("Alder Works's security review"), judge=DecisionJudge(both)) == []
    assert ties(store, fact("Vega"), judge=DecisionJudge(both)) == [("projects/vega", "subject")] and len(both.asked) == 1

    class Costly(DecisionJudge):
        def judge(self, fact: Fact, candidates: list[Identity]) -> Judged:
            return Judged([candidates[0].id], TokenCount(input_tokens=120, output_tokens=4), "glm")

    spent.clear()
    assert ties(store, fact("the Alder deal"), judge=Costly(decider), spent=spent) == [("things/alder-co", "subject")]
    assert spent[0].usage.input_tokens == 120


def test_a_record_binds_to_each_article_any_of_its_keys_or_header_names_binds() -> None:
    from cerebro.identity import bound_to

    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder Works", {"domain": ["alder.example"]}))
    store.create(Identity("people/ann", "people", "Ann", {"email": ["ann@birch.example"]}))
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    record = Record(id="m1", source="mail", revision="1",
                    keys={"domain": ["alder.example"], "email": ["ann@birch.example"], "project": ["Apollo"]})
    assert sorted(bound_to(store, record)) == ["people/ann", "projects/apollo", "things/alder"]


def test_a_header_line_of_several_names_links_a_fact_only_to_those_it_names() -> None:
    from cerebro.article import Identity
    from cerebro.connector import Record
    from cerebro.facts import Fact
    from cerebro.fakes import MemoryStore
    from cerebro.identity import bound_to, link

    store = MemoryStore()
    for id, title in (("accounts/brightwater", "Brightwater"), ("people/nessa-torvik", "Nessa Torvik"),
                      ("people/priya-nair", "Priya Nair")):
        store.create(Identity(id, id.split("/")[0], title))
    record = Record(id="local/call.md", source="local", revision="1",
                    keys={"account": ["Brightwater"], "attendees": ["Nessa Torvik, Priya Nair, Sam Okoye"]})
    assert sorted(bound_to(store, record)) == ["accounts/brightwater", "people/nessa-torvik", "people/priya-nair"]

    def fact(n: int, text: str, subject: str) -> Fact:
        return Fact(f"fact_{n}", text, subject, "fact", "", "local", "local/call.md")

    # a fact about Priya links to her and the account, never to Nessa, who sat in the call
    assert link(store, fact(1, "Priya Nair asked for SSO.", "Priya Nair"), record) == [
        "people/priya-nair", "accounts/brightwater"]
    # one whose text names Nessa links to her, whatever its subject
    assert link(store, fact(2, "The pilot starts once nessa torvik signs.", "Brightwater"), record) == [
        "accounts/brightwater", "people/nessa-torvik"]
    # a line of one name links every fact of the record
    one = Record(id="local/note.md", source="local", revision="1", keys={"account": ["Brightwater"]})
    assert link(store, fact(3, "The warehouse loses six hours a week.", "warehouse team"), one) == [
        "accounts/brightwater"]


def test_each_way_a_fact_links_says_how_it_is_tied() -> None:
    store = MemoryStore()
    store.create(Identity("people/ada", "people", "Ada Park", {"email": ["ada@example.com"]}))
    store.create(Identity("people/bo", "people", "Bo Lind", {"email": ["bo@example.com"]}))
    store.create(Identity("people/cy", "people", "Cy Moss"))
    store.create(Identity("things/alder", "things", "Alder Works"))
    store.create(Identity("projects/vega", "projects", "Vega"))
    store.create(Identity("projects/apollo", "projects", "Apollo"))
    note = Record(id="notes/1.md", source="local", revision="1",
                  keys={"email": ["bo@example.com"], "account": ["Alder Works"], "attendees": ["Ada Park, Cy Moss"]})
    said = Fact(id="f_said", text="Ada Park asked Cy Moss about Vega.", subject="Ada Park", kind="note", at="",
                source="local", record="notes/1.md",
                mentions=[Mention("Ada Park", "subject"), Mention("Vega", "about"), Mention("Cy Moss", "about")])
    link(store, said, note)
    assert store.ties_of("f_said") == {"people/bo": ["record"], "people/ada": ["subject"],
                                       "projects/vega": ["mentions"], "people/cy": ["mentions"],
                                       "things/alder": ["record"]}

    keyed = Fact(id="f_keyed", text="x", subject="Bo", kind="note", at="", source="mail", record="mail:2",
                 mentions=[Mention("Bo", "subject", {"email": ["bo@example.com"]}),
                           Mention("Ada", "about", {"email": ["ada@example.com"]})])
    link(store, keyed)
    assert store.ties_of("f_keyed") == {"people/bo": ["subject"], "people/ada": ["mentions"]}

    guessed = Fact(id="f_guessed", text="x", subject="Vega", kind="note", at="", source="mail", record="mail:3",
                   mentions=[Mention("Vega", "about"), Mention("Apollo", "about")])
    link(store, guessed)
    assert store.ties_of("f_guessed") == {"projects/vega": ["mentions"], "projects/apollo": ["mentions"]}


def test_a_near_match_ties_as_the_subject_words_do_and_a_merged_article_keeps_the_tie() -> None:
    store = MemoryStore()
    store.create(Identity("things/alder", "things", "Alder Works"))
    store.create(Identity("things/old-alder", "things", "Old Alder", status="merged"))
    store.append("merges", {"redirect": "things/old-alder", "to": "things/alder"})
    judge = DecisionJudge(FakeDecider({"things/alder": 0.9}))
    near = fact("Alder Works's security review")
    link_ties(store, near.id, ties(store, near, judge=judge))
    assert store.ties_of("f_Alder Works's security review") == {"things/alder": ["subject"]}
    guess = Fact(id="f_guess", text="x", subject="Alder Works's renewal", kind="note", at="", source="mail",
                 record="mail:1", mentions=[Mention("Alder Works's renewal", "about")])
    link_ties(store, guess.id, ties(store, guess, judge=judge))
    assert store.ties_of("f_guess") == {"things/alder": ["mentions"]}
    assert link_to(store, "f_told", ["things/old-alder"], how="record") == ["things/alder"]
    assert store.ties_of("f_told") == {"things/alder": ["record"]}
    assert store.rows("merges")[-1].data == {"at": "", "followed": "things/old-alder", "to": "things/alder",
                                             "fact": "f_told", "how": "record"}
