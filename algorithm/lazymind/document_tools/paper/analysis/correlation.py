"""Independent correlation reference; generated production programs do not call this."""
import math


def correlation(job, x, y):
    import numpy as np
    from scipy import stats

    n = len(x)
    op, test, level = job['operation'], job['correlation_test'], job['interval_level']
    if n < 2:
        raise ValueError('相关分析至少需要两个共同非缺失观测')
    if test and n < 3:
        raise ValueError('相关检验至少需要三个独立共同观测')
    if level is not None and (op != 'pearson' or n < 4):
        raise ValueError('当前仅支持至少四个独立观测的 Pearson Fisher-z 双侧区间')
    if test and op == 'spearman' and n <= 500:
        raise ValueError('小样本 Spearman 置换检验尚未注册；不允许用渐近 p 值替代，请仅报告描述系数')
    # Scaled, centered dot products independently check scipy.pearsonr/spearmanr.
    vectors = []
    for values in (x, y):
        if op == 'spearman':
            values = stats.rankdata(values, method='average')
        centered = values - values.mean()
        scale = float(np.max(np.abs(centered)))
        if scale == 0:
            raise ValueError('常数列的相关系数未定义；不能填零或生成对角线一')
        norm = float(np.linalg.norm(centered / scale))
        if not math.isfinite(scale) or not math.isfinite(norm):
            raise ValueError('相关分析数值范围导致不可验证的精度损失')
        if op == 'pearson' and scale * norm < np.finfo(float).eps ** .75 * abs(float(values.mean())):
            raise ValueError('近常数列的 Pearson 系数存在精度风险，不能静默接受')
        vectors.append(centered / scale / norm)
    value = float(np.clip(np.dot(*vectors), -1, 1))
    result = dict(value=value, lower=None, upper=None, statistic=None, p_value=None)
    if level is not None:
        if abs(value) == 1:
            lower = upper = value
        else:
            z = math.atanh(value)
            radius = float(stats.norm.ppf((1 + level) / 2) / math.sqrt(n - 3))
            lower, upper = math.tanh(z - radius), math.tanh(z + radius)
        result.update(lower=lower, upper=upper)
    if test:
        alternative = job['alternative']
        if op == 'pearson':
            dist = stats.beta(n / 2 - 1, n / 2 - 1, loc=-1, scale=2)
            p = (2 * dist.cdf(-abs(value)) if alternative == 'two_sided' else
                 dist.sf(value) if alternative == 'greater' else dist.cdf(value))
        else:
            t = math.copysign(math.inf, value) if abs(value) == 1 else value * math.sqrt((n - 2) / (1 - value ** 2))
            p = (2 * stats.t.sf(abs(t), n - 2) if alternative == 'two_sided' else
                 stats.t.sf(t, n - 2) if alternative == 'greater' else stats.t.cdf(t, n - 2))
        result.update(statistic=value, p_value=float(p))
    return result
