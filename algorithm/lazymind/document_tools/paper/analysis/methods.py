"""Describe the operations that actually ran, rather than unverified planning prose."""
DESCRIPTIONS = {
    'mean': '按每个任务锁定的分组与筛选范围计算有效观测均值。',
    'count': '统计锁定范围内响应列非空的记录数；记录数不自动等于独立样本量。',
    'pearson': '在锁定范围内按两个变量共同非缺失的原始观测计算 Pearson 线性相关系数，系数无量纲。',
    'spearman': '在锁定范围内按两个变量共同非缺失观测计算 Spearman 单调秩相关；并列值取平均秩。',
    'difference': '计算选中组均值减基线组均值，保留原始响应单位。',
    'paired_difference': '按锁定匹配键取共同实验单位，计算逐对的选中组减基线差值及其均值。',
    'paired_t': '对锁定匹配键确定的配对差执行 t 检验，检验方向以任务 alternative 为准。',
    'welch_t': '对锁定的独立两组执行 Welch t 检验，检验方向以任务 alternative 为准。',
    'ols_coefficient': '拟合包含截距的数值 OLS 回归，计算锁定系数；经典误差假设下的关联不代表因果。',
    'beta_mean': '在锁定的似然和已披露 Beta 先验下，计算解析后验均值。',
    'beta_probability': '在锁定的似然和已披露 Beta 先验下，计算指定阈值事件的解析后验概率。',
}


def executed_methods(plan):
    jobs = plan['jobs']
    operations = list(dict.fromkeys(job['operation'] for job in jobs))
    methods = [DESCRIPTIONS[operation] for operation in operations]
    if any(job['interval_level'] is not None and job['operation'] in
           ('mean', 'difference', 'paired_difference', 'paired_t', 'welch_t', 'ols_coefficient') for job in jobs):
        methods.append('按各任务锁定的置信水平计算双侧 Student t 区间；独立两组差采用 Welch 自由度。'
                       '区间方向不随单侧检验方向改变。')
    if any(job['interval_level'] is not None and job['operation'] == 'beta_mean' for job in jobs):
        methods.append('后验区间为各任务声明水平的 Beta 等尾可信区间。')
    if any(job['interval_level'] is not None and job['operation'] == 'pearson' for job in jobs):
        methods.append('Pearson 采用 Fisher-z 变换及标准误 1/sqrt(n-3) 的近似双侧置信区间。')
    if any(job.get('correlation_test') and job['operation'] == 'pearson' for job in jobs):
        methods.append('Pearson 零相关检验使用独立二元正态零假设下的精确 Beta 分布，方向按 alternative。')
    if any(job.get('correlation_test') and job['operation'] == 'spearman' for job in jobs):
        methods.append('Spearman 零秩相关检验使用大样本 t 近似，要求 n>500，方向按 alternative。')
    if not any(job['operation'] in ('paired_t', 'welch_t') or job.get('correlation_test') for job in jobs):
        methods.append('本轮未执行显著性检验或多重检验校正。')
    elif plan['multiplicity'] == 'holm':
        methods.append('对锁定检验族的原始 p 值应用 Holm 多重检验校正。')
    if any(job['missing_policy'] == 'complete_case' for job in jobs):
        methods.append('完整观测筛选按各任务实际需要的字段执行；配对任务取有效共同键，缺失值不填为零。'
                       '有效样本量及排除数量分别记录。')
    return methods
