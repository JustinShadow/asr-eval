"""数据集导入器。"""

from app.importers.aishell1 import Aishell1Importer
from app.importers.aishell3 import Aishell3Importer
from app.importers.alimeeting import AlimeetingImporter
from app.importers.apy import ApyImporter
from app.importers.kespeech import KespeechImporter
from app.importers.magicdata import MagicDataImporter

__all__ = [
    "Aishell1Importer",
    "Aishell3Importer",
    "AlimeetingImporter",
    "ApyImporter",
    "KespeechImporter",
    "MagicDataImporter",
]
