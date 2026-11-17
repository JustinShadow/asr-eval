"""前端展示用的静态枚举与中文标签（M1 §3.3、§7.2；M7 §5.1）。"""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import require

router = APIRouter(prefix="/api/v1/meta", tags=["metadata"])

_ENUM_LABELS: dict[str, dict[str, str]] = {
    "languages": {
        "zh": "普通话",
        "yue": "粤语",
        "wuu": "吴语（沪语）",
        "xiang": "湘语",
        "gan": "赣语",
        "sichuan": "四川话",
        "minnan": "闽南语",
        "hak": "客家话",
        "en": "英语",
    },
    "dialects": {
        "Beijing": "北京",
        "Ji-Lu": "冀鲁",
        "Jiang-Huai": "江淮",
        "Jiao-Liao": "胶辽",
        "Lan-Yin": "兰银",
        "Mandarin": "普通话",
        "Northeastern": "东北",
        "Southwestern": "西南",
        "Zhongyuan": "中原",
    },
    "genders": {"M": "男", "F": "女", "unknown": "未知"},
    "age_groups": {
        "A": "14 岁以下",
        "B": "14~25 岁",
        "C": "26~40 岁",
        "D": "41 岁以上",
    },
    "splits": {"train": "训练集", "dev": "开发集", "test": "测试集", "all": "全部"},
    "metrics": {
        "cer": "字错误率（CER）",
        "numeric_cer": "数字相关 CER（近似）",
        "wer": "词错误率（WER）",
        "punct_f1": "标点 F1",
        "sentence_end_acc": "句末标点准确率",
        "itn_acc": "ITN 实体准确率",
        "der": "说话人分离错误率（DER）",
        "sa_der": "说话人关联分离错误率（SA-DER）",
        "naming_acc": "声纹命名准确率",
        "speaker_eer": "说话人等错误率（EER）",
        "ttft_ms": "首字延迟",
        "finalize_ms": "最终结果延迟",
        "rtf_dev": "非流式RTF",
        "rtf_e2e": "非流式E2E RTF",
        "rtf_realtime": "流式RTF",
        "overhead_ratio": "设备开销比",
        "throughput": "音频吞吐率",
    },
    "reasons": {
        "no_reference_text": "该数据集没有参考文本",
        "no_sentence_punctuation": "参考缺少句末标点",
        "no_itn_reference": "参考无 ITN 对照",
        "no_segment_timeline": "参考无时间戳分段",
        "no_speaker_labels": "参考无说话人标签",
        "device_no_speaker_output": "设备输出不含说话人信息",
        "device_no_punctuation_output": "设备输出不含标点",
        "device_no_naming": "设备未回显声纹匹配结果",
        "enrollment_missing": "缺少说话人注册音频",
        "staging_unavailable": "无法向设备暂存注册音频",
        "audio_too_long": "音频超过设备时长上限",
        "unsupported_language": "设备不支持该语言",
        "insufficient_coverage": "有效样本覆盖率不足",
        "no_scoreable_cases": "没有可评分样本",
        "no_numeric_reference": "参考中没有数字或数词",
        "low_coverage": "有效样本覆盖率不足",
        "metric_disabled": "该指标已在评分口径中关闭",
        "empty_reference": "参考文本为空",
        "no_diarization_output": "设备未输出说话人分段",
        "no_voiceprint_match": "设备未返回有效声纹匹配",
        "insufficient_trials": "声纹试验样本不足，无法计算 EER",
        "no_itn_entities": "参考中没有可评测的 ITN 实体",
        "no_punct_reference": "数据集没有标点参考",
        "device_no_punctuation": "设备未声明支持标点输出",
        "device_no_itn": "设备未声明支持 ITN",
        "no_speaker_reference": "数据集没有说话人参考",
        "observed_missing": "设备能力尚未完成探测",
        "not_probed": "设备尚未完成探测",
        "no_partial_timing": "没有可用的流式首字时延数据",
        "no_realtime_timing": "没有可用的实时最终结果时延数据",
        "no_inference_timing": "没有可用的设备推理时延数据",
        "no_batch_timing": "没有可用的批处理端到端时延数据",
        "no_realtime_results": "没有可用的实时模式结果",
        "cost_data_unavailable": "缺少设备成本数据",
        "no_wall_timing": "缺少墙钟时长数据，无法计算吞吐率",
        "empty_bucket": "该分桶没有有效参考文本",
        "no_hypothesis": "缺少设备识别结果",
        "no_reference": "缺少参考文本",
        "no_common_samples": "对比项之间没有共同样本",
        "aggregate_metric_unavailable": "聚合指标不可用",
    },
}


@router.get("/enums")
def get_enums(
    _: dict[str, Any] = Depends(require("meta:read")),
) -> dict[str, dict[str, str]]:
    """返回代码到中文显示名的映射，供数据筛选与指标状态展示使用。"""
    return {category: labels.copy() for category, labels in _ENUM_LABELS.items()}
