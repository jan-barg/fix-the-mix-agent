"""Structured data the agents exchange (DESIGN.md §4.1, §7.2, §7.3): the brief, researcher results and
the Lead's tool arguments. Tool definitions for the Lead are generated from these models.

No length limits live in the JSON schemas (not every provider's structured output accepts them);
they are checked in code instead.
"""

from typing import Literal

from pydantic import BaseModel, Field

Specialist = Literal["kick", "low_end", "tonal_loudness", "stereo"]
SPECIALISTS: tuple[str, ...] = ("kick", "low_end", "tonal_loudness", "stereo")
SCOPES_HELP = ('Scope references: "user" (a whole track), "ref1/drop 1" (a confirmed section, as listed by '
               'get_run_overview) or "user@60.0-90.0" (any passage, in seconds).')


# --- Brief ---


class ReferenceRole(BaseModel):
    track_id: str = Field(description='"ref1", "ref2" or "ref3".')
    aspects: list[str] = Field(description='What to take from this reference, e.g. "kick and bass weight", "build-to-drop '
                                           'loudness". Empty for a general comparison.')
    exclusions: list[str] = Field(description='What not to treat as a target, e.g. "its brighter top end".')


class CombinedTarget(BaseModel):
    contributors: list[str] = Field(description="Track IDs averaged or combined.")
    aspects: list[str]
    method: str = Field(description='In plain words, e.g. "average tonal balance of the drops".')


class Brief(BaseModel):
    goals: list[str] = Field(description="The producer's goals and stated concerns, in their own terms.")
    preserve: list[str] = Field(description="Qualities to keep.")
    reference_roles: list[ReferenceRole]
    comparison_mode: Literal["separate", "aspects", "average"] = Field(
        description="separate: each reference on its own; aspects: different aspects from different references; "
                    "average: one combined target.")
    combined_target: CombinedTarget | None = None
    focus_sections: list[str] = Field(description="Section labels or timestamps the producer mentioned, e.g. \"drop\".")
    production_stage: Literal["mix", "master", "unknown"]
    assumptions: list[str] = Field(description="Defaults you chose where the comments were silent.")


# --- Researcher result (SPEC §7.3) ---


class Value(BaseModel):
    name: str = Field(description='The scalar\'s exact name as listed in the evidence: the text before " = ".')
    value: float = Field(description="As shown in the evidence, or rounded.")
    unit: str
    evidence_id: str


class Observation(BaseModel):
    text: str = Field(description="A measured fact in plain language. No evidence IDs in the text.")
    evidence_ids: list[str]
    values: list[Value] = Field(description="Every number the text states, each tied to its scalar.")


class Comparison(BaseModel):
    target: str = Field(description='Which reference (or the combined target) this compares against, e.g. "Reference 1".')
    text: str
    evidence_ids: list[str]


class Interpretation(BaseModel):
    text: str = Field(description="A possible cause, worded as a hypothesis.")
    alternatives: list[str] = Field(description="Other explanations the evidence does not rule out.")


class Experiment(BaseModel):
    action: str = Field(description="A concrete change to try in the producer's session.")
    how_to_verify: str = Field(description="How to check the result: what to re-measure, on which passage.")
    supports_goal: str = Field(description="Which of the producer's goals this serves.")


class FollowUpRequest(BaseModel):
    need: str = Field(description="The evidence that is missing.")
    why: str = Field(description="What it would resolve.")
    suggested_tool: str
    suggested_scopes: list[str]


class ResearcherResult(BaseModel):
    question: str
    scope: str = Field(description="The tracks and passages examined, in plain words.")
    observations: list[Observation]
    comparisons: list[Comparison]
    interpretations: list[Interpretation]
    experiments: list[Experiment]
    preserve: list[str] = Field(description="What already matches the references or the goals and should be kept.")
    limitations: list[str]
    confidence: str = Field(description="Plain-language confidence and why. No numeric scores.")
    follow_up_requests: list[FollowUpRequest]


# --- Lead tool arguments ---


class NoArgs(BaseModel):
    pass


class Scopes(BaseModel):
    scopes: list[str] = Field(description=SCOPES_HELP)


class TonalArgs(Scopes):
    mode: Literal["peak_normalised", "level_matched"] = Field(
        "peak_normalised", description="level_matched is a secondary view for tonal shape only, never for loudness.")
    combine: list[str] = Field(default_factory=list,
                               description="Reference scopes (from `scopes`) to average into one combined target.")


class LowEndArgs(Scopes):
    bands: list[Literal["sub", "low", "upper_bass", "low_mid"]] = Field(
        default_factory=list, description="Bands for the recovery analysis; empty for all four.")


class SpectrogramArgs(Scopes):
    preset: Literal["overview", "low_detail", "transient"] = "overview"
    fmin: float = 20.0
    fmax: float | None = None


class KickCandidateArgs(BaseModel):
    track: str = Field(description='"user", "ref1", ...')
    within: list[str] = Field(default_factory=list,
                              description="Optional section or passage references to search inside, e.g. [\"user/drop 1\"].")
    n: int = Field(3, description="How many candidates (1-5).")


class KickPick(BaseModel):
    track: str
    time_s: float = Field(description="Onset time of a kick candidate, as listed by find_kick_candidates.")


class AnalyseKickArgs(BaseModel):
    kicks: list[KickPick] = Field(description="One kick per track to compare, usually each track's best candidate.")


class RunResearcherArgs(BaseModel):
    specialist: Specialist
    question: str = Field(description="One focused question tied to the producer's goals.")
    scopes: list[str] = Field(default_factory=list, description=SCOPES_HELP + " Empty: the default focus (the first "
                                                                               "matched drops) plus whole tracks.")
    user_intent: str = Field(description="The part of the brief this question serves, including reference roles.")
    evidence_ids: list[str] = Field(default_factory=list,
                                    description="Evidence you already have that the researcher should see as well.")


class Rejection(BaseModel):
    finding_ref: str
    reason: str


class Priority(BaseModel):
    title: str
    why: str = Field(description="Why it matters for the producer's goals.")
    finding_refs: list[str]


class FinishResearchArgs(BaseModel):
    accepted: list[str] = Field(description='Finding references you accept, e.g. "t_04/o2", "t_04/x1".')
    rejected: list[Rejection]
    priorities: list[Priority] = Field(description="At most three, most important first.")
    preserve: list[str]
    limitations: list[str] = Field(description="What could not be established, and why.")
    summary: str = Field(description="A short plain-language summary for the producer: the top priorities and what "
                                     "to keep. No internal IDs.")


# --- Report (DESIGN.md §7.4) ---


class Claim(BaseModel):
    name: str = Field(description='The scalar\'s exact name, as listed under "Measured values": the text before " = ".')
    value: float = Field(description="As shown, or rounded.")
    unit: str
    evidence_id: str


ReportSection = Literal["overview", "preserve", "general", "kick_low_end", "stereo", "section_by_section",
                        "method_notes", "limitations"]
REPORT_SECTIONS: tuple[str, ...] = ("overview", "preserve", "general", "kick_low_end", "stereo", "section_by_section",
                                    "method_notes", "limitations")


class Para(BaseModel):
    section: ReportSection
    text: str = Field(description="Plain prose for the producer. No IDs, no Markdown.")
    claims: list[Claim] = Field(description="Every number with a unit in the text, each tied to its scalar.")
    figure_ids: list[str] = Field(description="Figures to show after this paragraph (fig_...), usually zero or one.")


class ReportPriority(BaseModel):
    title: str = Field(description="The change, in a few words.")
    why: str = Field(description="Why it matters for the producer's goals.")
    observation: str = Field(description="What was measured, plainly.")
    interpretation: str = Field(description="What could cause it, worded as a hypothesis. Empty if none was accepted.")
    experiment: str = Field(description="What to try in the session.")
    verification: str = Field(description="How to check the result.")
    claims: list[Claim] = Field(description="Every number with a unit in title, why, observation, interpretation "
                                            "and verification.")
    figure_ids: list[str]


class ReportDraft(BaseModel):
    """One flat list of paragraphs, each naming its section: a schema with a field per section is too
    large for strict structured output."""

    priorities: list[ReportPriority] = Field(description="The Lead's priorities, in its order. At most three.")
    paragraphs: list[Para] = Field(description="Every other paragraph of the report, in reading order.")

    def section(self, name: str) -> list[Para]:
        return [p for p in self.paragraphs if p.section == name]


class FixedBlock(BaseModel):
    id: str = Field(description='The block ID from the list of problems, e.g. "priority2.observation" or "paragraphs.5".')
    text: str
    claims: list[Claim] = Field(description="Every claim the corrected text needs.")


class FixedBlocks(BaseModel):
    blocks: list[FixedBlock] = Field(description="Only the blocks that had problems, corrected.")
    new_paragraphs: list[Para] = Field(description="Paragraphs for required sections that were missing; else empty.")


class EditedBlock(BaseModel):
    id: str
    text: str


class EditedBlocks(BaseModel):
    blocks: list[EditedBlock]


# --- Schema helpers ---


def inline_schema(model: type[BaseModel]) -> dict:
    """The model's JSON schema with every $ref expanded and titles removed, for tool definitions."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def expand(node, is_properties: bool = False):
        if isinstance(node, dict):
            if "$ref" in node and not is_properties:
                return expand(defs[node["$ref"].rsplit("/", 1)[-1]])
            # In a "properties" map every key is a field name (Priority has a field called "title").
            return {k: expand(v, k == "properties" and not is_properties) for k, v in node.items()
                    if is_properties or k != "title"}
        if isinstance(node, list):
            return [expand(v) for v in node]
        return node

    return expand(schema)
