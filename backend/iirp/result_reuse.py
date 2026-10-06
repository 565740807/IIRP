"""Find an already published result without decoding its payload."""
from sqlalchemy import select
from sqlalchemy.orm import load_only

from iirp.business_models import AnalysisResult


def result_identity(s, analysis_id, security_id, input_key):
    return s.scalar(select(AnalysisResult).options(load_only(
        AnalysisResult.id, AnalysisResult.analysis_id, AnalysisResult.security_id,
        AnalysisResult.input_key, AnalysisResult.inputs,
    )).where(AnalysisResult.analysis_id == analysis_id,
             AnalysisResult.security_id == security_id, AnalysisResult.input_key == input_key))

