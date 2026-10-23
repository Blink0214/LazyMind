"""Reuse approved plotting skills and Docker renderer for verified v2.1 findings."""
import json
import copy
import os
from contextlib import contextmanager
from concurrent.futures import as_completed

from .model import call_code_model
from .api import reference as api_reference
from .pipeline import draw_one, limits
from .code import ALLOWED_IMPORTS, FORBIDDEN, FORBIDDEN_ATTRIBUTES
from .skills import PlotSkills
from .style_profiles import bind_series
from .bindings import bind_context, finding_table, figure_semantics, result_view
from .figure_data import prepare_figure_data
from ..analysis.prompt_payload import figure_statistics, figure_spec, drawing_reference, native_interface
from .code_response import code_response, CODE_INSTRUCTION, REPAIR_INSTRUCTION
from .templates import NATIVE_FUNCTION_GUIDANCE
from ..analysis.schema import digest
from ..execution.threads import ThreadPoolExecutor
from ..analysis import trace as diagnostics


AXES_GUIDANCE = '''For every plt.subplots call use squeeze=False, then axes = axes.ravel().
axes[i] is always one Axes, including a single panel. Pass axes[i], never the axes array, to native helpers.
'''


BOUND_GUIDANCE = {
    'bar_grouped_hatch': '''Use grid['values'] as data_dict, grid['methods'] as labels and grid['bar_colors'] as colors.
Use range(len(grid['categories'])) for x_positions, total_w=.78, xlim=(-.56,len(grid['categories'])-.44),
benchmarks=grid['categories'], y_limits=grid['bar_limits'], legend_anchor=layout['legend_anchor'].
Use // hatch only for an explicitly requested focal method; otherwise empty hatches and best_method=None.
Pass intervals={name: [triple[1:] for triple in triples] for name, triples in grid['summaries'].items()}.
The helper draws those verified endpoints and places value labels above them; do not add duplicate whiskers.
For one categorical series use grid['category_styles']; for separate series pass category_styles=None.''',
    'point_interval': '''Use grid['summaries'], grid['methods'], grid['categories'],
grid['colors'], grid['edges'] and grid['markers'].
For arithmetic differences use grid['difference_limits'], then call label_difference_axis with grid['comparisons'].
Use its returned legend labels, which state the full subtraction. Supply better/direction_label only when known.
For a shared legend use fig.legend(handles, labels, loc='upper center', ncol=len(labels)).
Point-interval layout has no legend_anchor key; the renderer fits the shared legend into its reserved strip.
Include explicitly requested practical references in the numeric limits.
For absolute estimates use grid['value_limits'].
Missing endpoints are not zero; do not draw invented intervals.''',
    'line_confidence_band': '''Use grid['curves'] as series, with grid['methods'], grid['colors'], grid['edges'],
grid['markers'] and grid['line_styles'] as aligned lists.
Use numeric grid['x_limits'] and grid['value_limits']. Each curve contains (x,mean,lower,upper) in ascending x order.
Only observed x values are present. Do not resample, smooth or extrapolate.''',
    'heatmap_matrix': '''For correlation pairs use spec.style_contract.quantity_semantics.pair_rectangle directly:
values, row_labels, column_labels; mode='correlation_pairs', palette='PuOr', signed=True, limits=(-1,1), format='.2f'.
It is a verified rectangle, not a full symmetric correlation matrix. Other correlation matrices use the supplied
quantity_semantics.matrices, leaving uncomputed cells NA. Never invent a diagonal or additional correlations.
Choose the matching style_contract.heatmap_layouts entry: figsize=tuple(entry['inches']),
fig.add_axes(entry['panel']) and fig.add_axes(entry['colorbar']). These keys are not figsize/axes/cbar.
Bind ax = fig.add_axes(entry['panel']) and colorbar_ax = fig.add_axes(entry['colorbar']).
After image = draw_heatmap(ax, ...), call draw_colorbar(fig, image, colorbar_ax, label, ticks).
Never pass ax as the colorbar axis: that would overwrite the data panel. The host fits long English
column labels to their measured cell width without shrinking fonts; keep the exact supplied names.
Call draw_colorbar with label EXACTLY 'Pearson r' or 'Spearman rho'. For other matrix values use the declared
quantity semantics and one allowed colormaps[mode][0] string. Color does not encode statistical significance.''',
}


def generate_bound_code(table, analysis, spec, reference, previous, error):
    """Ask for scientific presentation code, with host-owned identity binding and a small API."""
    prompt = CODE_INSTRUCTION + NATIVE_FUNCTION_GUIDANCE + AXES_GUIDANCE + '''Implement draw(data, statistics, spec) -> Figure for ONLY this planned figure.
Return the executable source directly and an accurate caption in the optional text block. 只做合理推理，不要过度思考。
The host validates findings, scopes, dimensions, intervals and group identity, and preloads the native functions.
Use result_grid(data, metric, series_column, category_column, where) -> dict for data mapping; do not implement readers,
ID parsers, schema/identity guards or a second validation framework. Choose actual qualified dimension names below.
where is {} or exact coordinate/operation filters. Each cell must be unique; facet rather than averaging results.
result_grid returns a PLAIN DICTIONARY, never an object, namespace or DataFrame.
Access fields ONLY by string keys: grid['methods'], grid['values'], grid['curves'], grid['unit'].
Attribute syntax grid.methods/grid.values/grid.curves is WRONG. All appearance fields are aligned LISTS,
not dictionaries: grid['colors'][i] matches grid['methods'][i]. Do not add hasattr/getattr compatibility branches.
Example: grid = result_grid(data, metric, series_column, category_column, {});
methods = grid['methods']; values = grid['values']; colors = grid['colors']; unit = grid['unit'].
The dictionary keys are methods (artist identities), display_labels, categories, x, values[method],
summaries[method]=[[estimate,lower,upper],...], curves[method]=[x,mean,lower,upper],
colors, bar_colors, edges, markers, line_styles, comparisons[method]=[selected_name,baseline_name],
value_limits, difference_limits (include zero), bar_limits (include zero), x_limits, unit and sample_sizes.
All arrays bind runtime verified data; never hardcode preview measurements. Empty cells remain None.
Native function signatures below are authoritative. Call them directly; do not redefine native or adapter functions.
Use data['metadata']['result_records'] only for an uncommon mapping not covered by result_grid.
No statistics/inference, file/network access, random data, rcParams setup, top-level calls, decorators or default
arguments. All three draw arguments are dictionaries. Imports/functions/literal constants only at top level.
For one panel use plt.subplots(1,1,figsize=tuple(layout['single_inches']),squeeze=False),
where layout=spec['style_contract']['layout'], then fig.subplots_adjust(**layout['single_panel']).
For two use plt.subplots(1,2,figsize=tuple(layout['two_inches']),squeeze=False) and layout['two_panels'].
Finalize margins before native calls; the renderer fits native legends outside observations.
Titles are metadata only: Chart.title is returned separately. Draw no overall figure title; do not call
suptitle or add title text with fig.text/ax.text. Use the host's short scope label for prepared panels; otherwise pass '' or a subplot identifier.
Keep short scope labels on panels and explanations in caption; retain axis names, units and legends.
Keep fonts/styles unchanged. Use native appearance
from grid identities, with display_labels for legend text. Preserve every selected finding and its sign/units.
Captions state actual interval meaning, n/missing handling and synthetic-data status. No unsupported significance
claims, causal claims, extra panels, changed endpoints or invented observations.
Use English for all visible chart text and any returned caption, including repair responses.
''' + BOUND_GUIDANCE[spec['style']] + '\n'
    if previous:
        prompt += REPAIR_INSTRUCTION + 'A repair response may also include the corrected caption.\n'
    compact_spec = figure_spec(spec, table)
    contract = compact_spec['style_contract']
    for field in ('rc', 'rules', 'series_colors', 'bar_series_colors', 'series_edges', 'series_markers',
                  'series_line_styles', 'series_sizes'):
        contract.pop(field, None)
    metadata = table['metadata']
    fields = ('result_records', 'result_dimensions', 'user_request', 'source_description')
    context = figure_statistics(analysis)
    payload = dict(data={'metadata': {key: metadata[key] for key in fields}},
                   context={key: context.get(key, []) for key in ('methods', 'limitations', 'models')},
                   spec=compact_spec, native_api=native_interface(reference, spec),
                   execution_rules={'allowed_imports': sorted(ALLOWED_IMPORTS),
                                    'forbidden_names': sorted(FORBIDDEN),
                                    'forbidden_attributes': sorted(FORBIDDEN_ATTRIBUTES)},
                   previous_code=previous, error=error)
    result = call_code_model(prompt + '\nINPUT_JSON:\n' + json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    code = code_response(result, previous, metadata=('caption',))
    if 'caption' in result:
        if not isinstance(result['caption'], str) or not result['caption'].strip():
            raise ValueError('图注必须为非空文本')
        spec['caption'] = result['caption']
    return code


def generate_code(table, analysis, spec, reference, previous, error):
    if table.get('figure', {}).get('panels'):
        return generate_prepared_code(table, spec, reference, previous, error)
    if (spec.get('style') in BOUND_GUIDANCE and spec.get('style_contract', {}).get('version') == 'paper-v1'
            and table.get('metadata', {}).get('result_records')):
        return generate_bound_code(table, analysis, spec, reference, previous, error)
    native_guidance = NATIVE_FUNCTION_GUIDANCE if spec.get('style') != 'custom' else ''
    prompt = CODE_INSTRUCTION + native_guidance + AXES_GUIDANCE + '''Include an accurate figure caption in the optional text block.
只做合理推理，不要过度思考。
Use only task-relevant reasoning; do not overthink. Return the executable source directly.
Implement draw(data, statistics, spec) -> Figure.
The native helper name is spec['style_contract']['native_template'], NEVER spec['native_template'].
The host already validated the native template. Bind its inputs directly; do not invent template-identity guards.
Call the declared native helper by its actual function name. The host already validates IDs, scopes, counts and
interval metadata; do not duplicate that validation framework or compare the same numbers from data and statistics.
All three arguments are JSON dictionaries, with nested dictionaries/lists/scalars, never attribute objects.
data['columns'] is a list of column names; data['rows'] is a list of equally wide row lists.
Use records = [dict(zip(data['columns'], row)) for row in data['rows']] and dictionary indexing/.get.
metadata.source_groups is a list of dictionaries, finding_scopes and style_contract are dictionaries.
Do not write object/dictionary compatibility readers. Dynamic attribute reflection, including getattr, vars,
setattr and delattr, is forbidden even in unused branches. Do not replace one with another on repair.
execution_rules below comes directly from the actual validator; obey the complete rules before generating code.
runtime_api supplies the actual Matplotlib signatures, legend object types and minimal call recipes. Follow it
on the FIRST response. BarContainer is not a Patch: never call its set_linewidth; use legend.get_patches() for
bar legend key borders. Use get_label(), not .label; legacy legendHandles does not exist in the pinned runtime.
Draw ONLY the requested figure from already VERIFIED findings. data.columns/rows contain finding_id/value/lower/upper
During early code generation, numbers may be host reference previews for the locked plan. Your code must bind
runtime values, never hardcode preview numbers. Execution occurs only after the generated statistics pass independent
verification, using those actual verified results with the same structure. No plotting execution happens in advance.
plus exact grouping columns such as runs.method/runs.benchmark/baseline.runs.method and metric/unit/n/n_unit.
Use these actual columns to filter/pivot result cells; they are already materialized, so no ID parsing is needed.
metadata.finding_scopes maps exact finding IDs to their verified filters and metric. Use this mapping to select
cells by their actual method/benchmark values; NEVER construct or guess a finding ID from a naming pattern.
metadata.source_groups supplies original group orders and artist_labels. style_contract includes their
exact appearance. Use each group's supplied artist_labels for series, and display_labels for the legend.
Namespaced labels disambiguate identical names in different source columns. Keep the same method color across
benchmarks. A finding ID identifies a result, not a distinct method requiring a new color.
statistics is the selected-finding view of Analysis; read interval.kind/level/method, sample units and probability
events from each finding. Null interval endpoints do NOT mean zero. Do not recompute inference, resample, smooth,
invent observations or substitute p values for posterior probabilities. Preserve identity when sorting labels/colors.
Use the supplied scientific style and copy/call any required native template helpers in the style reference. Preserve
the actual template geometry, fonts, colors and layout. Use spec.style_contract; never save/show figures or access
files/network. Top-level imports, literal constants and functions only. Bar value axes include zero. If a native
template is not applicable, report the incompatibility by raising ValueError; do not silently drop requested content.
All value/interval coordinates must come from data or statistics, never hardcoded numbers from examples.
The host calls draw. Do NOT add a top-level call such as fig=draw(data, statistics, spec), plt.figure() or plt.show().
For paper-v1 native styles, the host loads the reviewed template functions unchanged. Return draw and your
data-mapping helpers only; call the supplied native function with its exact signature. Do not redefine or modify
template helpers, add local imports inside them, or replace their palette/interval logic.
For two panels: layout = spec['style_contract']['layout'];
fig, axes = plt.subplots(1, 2, figsize=tuple(layout['two_inches']), squeeze=False);
axes = axes.ravel(); fig.subplots_adjust(**layout['two_panels']).
two_inches and two_panels are siblings, not nested. Finalize size and margins before native helper calls.
For native bar helpers, legend_anchor is layout['legend_anchor'], NOT style_contract['legend_anchor'].
For draw_paired_panel with positive bars, ylim=(0,max(1.0,1.2*max(actual_values_and_interval_uppers)))
reserves safe headroom. Do not iterate toward the minimum label boundary, where roundoff can fail the helper check.
Every function needs explicit arguments, with NO default arguments and NO decorators. Keep all drawing inside draw
or its helpers. No private attributes or imports from the external skill checkout; copy the supplied helper source.
Host applies style rc before draw; no rcParams setup is needed. Preserve the supplied native helper, and bind data
through its arguments. Do not add a panel or metric absent from this figure's selected findings.
For matrices and numeric fields, follow the explicit value colormap instead of method identity colors.
style_contract.colormaps maps each mode to a LIST of allowed palette NAMES. draw_heatmap's palette argument
requires ONE STRING, e.g. spec['style_contract']['colormaps']['correlation_pairs'][0], never the whole list.
Native stacked bars identify components with the supplied component_colors palette, consistently across panels.
Histogram/ECDF/violin, binary ROC/PR and hexbin require raw observations or predictions. The supplied findings
are aggregate results: do not fabricate raw vectors or use these styles when the required observations are absent.
Contouring requires an actual complete coordinate grid of finding values. Heatmap row normalization takes raw
confusion counts once, never already-normalized percentages. Matrix mode excludes the rejected Blues palette.
If the figure asks for intervals, draw the supplied finite endpoints as error bars/bands and allocate headroom;
do not omit them while claiming them in a caption. Null endpoints are missing, never zero.
For bars, place interval whiskers at the actual BarContainer patch centers. Move existing helper value labels
above each supplied interval upper endpoint; do not leave a duplicate label at the bar top beneath a whisker.
Reserve a separate top strip for a compact horizontal legend when all categories have tall bars. Legends must
cover no bars, interval whiskers or labels. Keep long explanatory captions in the separate caption, outside the canvas.
The renderer places native bar/point legends outside the data area and fits margins to the final font metrics.
Titles are metadata only: Chart.title is returned separately. Draw no overall figure title; do not call
suptitle or add title text with fig.text/ax.text. Use the host's short scope label for prepared panels; otherwise pass '' or a subplot identifier.
Keep short scope labels on panels and explanations in caption; retain axis names, units and legends. Do not shrink fonts.
spec.style_contract.quantity_semantics comes from VERIFIED findings. For posterior parameter means its
reference_values are thresholds on the horizontal probability axis, not categorical y positions. The host draws
these vertical references; do not add your own threshold line or annotation. For posterior-event heatmaps the
host uses quantity_semantics.value_label on the colorbar: P(p > threshold | data) is not a posterior mean or p value.
Use draw_colorbar for the supplied heatmap scale. Give it the declared value_label. For posterior-event matrices
use limits (0,1), mode='matrix', and number_format='probability' to avoid rounding tiny/tail probabilities to 0/1.
Never change these semantics.
For quantity_semantics.value_kind=correlation_coefficient, use variable_x/variable_y/correlation_method and scope
columns to build cells, not metric alone or guessed finding IDs. Separate methods and filtered cohorts into separate
matrices/panels. Mirror an actual verified off-diagonal coefficient without rerunning a test. A diagonal value requires
an explicit self-correlation finding; otherwise leave it NA. Missing cells are NA, not zero. Use mode='correlation',
palette='PuOr', signed=True, limits=(-1,1), number_format='.2f', and a colorbar named Pearson r or Spearman rho.
State pairwise missing-value handling and actual per-cell n (or an honest range with a cell-n table in the analysis).
Color encodes coefficient, not significance; no significance stars or causal descriptions. Do not mix correlation
coefficients with means, effect differences or p values in a single matrix. Do not invent confidence bands per cell.
The host verifies each rendered heatmap matrix against quantity_semantics.matrices, including labels, missing cells
and coefficients. Use the exact variable column names for tick labels; do not rename/abbreviate them. Use draw_colorbar
with label EXACTLY 'Pearson r' or 'Spearman rho' for the appropriate matrix; do not add math markup to that label.
If quantity_semantics.pair_rectangle is supplied, it is an ALTERNATIVE verified rectangular view of ALL these cells:
one shared anchor variable against outcomes, optionally with rows for exact cohort labels. You may call draw_heatmap
with mode='correlation_pairs' and its supplied values/row_labels/column_labels, using PuOr, signed=True and (-1,1).
Use correlation layout with enough width for the actual labels. Name the anchor variable in the separate caption.
Do not force a sparse set of anchor-outcome pairs into a mostly-empty square matrix or invent uncomputed pairs.
The caption must describe the actual plot, observed sample sizes/missing cells and interval meaning. Follow
source_description for synthetic-data disclosure. Correct stale color descriptions in the proposed caption;
do not claim p values or tests when finding.test is null. User-designated focal methods are in metadata.user_request.
'''
    if previous:
        prompt += REPAIR_INSTRUCTION + 'A repair response may also include the corrected caption.\n'
    payload = dict(data=table, statistics=figure_statistics(analysis), spec=figure_spec(spec, table),
                   execution_rules={'allowed_imports': sorted(ALLOWED_IMPORTS),
                                    'forbidden_names': sorted(FORBIDDEN),
                                    'forbidden_attributes': sorted(FORBIDDEN_ATTRIBUTES),
                                    'arguments_are_json_dicts': True,
                                    'top_level_calls_allowed': False,
                                    'decorators_or_default_arguments_allowed': False},
                   runtime_api=api_reference(spec), style_reference=drawing_reference(reference, spec),
                   previous_code=previous, error=error)
    result = call_code_model(prompt + '\nINPUT_JSON:\n' + json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    code = code_response(result, previous, metadata=('caption',))
    if 'caption' in result:
        if not isinstance(result['caption'], str) or not result['caption'].strip():
            raise ValueError('图注必须为非空文本')
        spec['caption'] = result['caption']
    return code


def generate_prepared_code(table, spec, reference, previous, error):
    """The model arranges the figure; the host supplies every native data/axis argument."""
    prompt = CODE_INSTRUCTION + NATIVE_FUNCTION_GUIDANCE + AXES_GUIDANCE + '''Implement draw(data, statistics, spec) -> Figure for this planned figure.
Use task-relevant reasoning only. Return source directly, optionally with an accurate caption.
data['figure'] is paper.figure.v1: points contain verified estimate/interval.low/interval.high and provenance;
panels contain the complete arguments for the supplied native function. Do not read statistics or other data keys.
For EVERY panel call the native function as native_function(ax, **panel['arguments']) exactly once.
Use the actual native_api function name, not the placeholder native_function. Do not rebuild, replace, mutate,
filter, average or hardcode those arguments. The host verifies native calls against the original bound data.
The arguments already include the correct numeric/category axes, labels, unit, intervals, colors and markers.
Category appearances are identity-bound too: pass through category_styles when supplied. Keep the host's short
panel scope labels unchanged; they are different from the full external figure title. Do not add a redundant Estimate legend
when the category ticks already identify all groups. A legend is needed when several series share the categories.
No result_grid call or result_records parsing is needed or available. Missing intervals remain absent, never zero.
You choose the Matplotlib panel arrangement, using the supplied paper layout and native functions unchanged.
For one panel use layout=spec['style_contract']['layout']; fig, axes = plt.subplots(1,1,figsize=tuple(layout['single_inches']),squeeze=False);
axes = axes.ravel(); fig.subplots_adjust(**layout['single_panel']); for two use layout['two_inches'] and layout['two_panels'].
For more panels do not multiply the paper width by the panel count. Keep layout.two_inches[0] as the maximum width; use readable spacing without shrinking fonts. Never truncate the panel list.
Place all panels. Keep their finding scopes distinct; do not add or drop panels. No statistical computation,
file/network access, rcParams changes, dynamic reflection or example data. The full figure title is separate metadata. Keep the host-provided short scope labels, which identify
the data in each panel. Explain interval kind/level in the caption.
For prepared point_interval/scatter_tradeoff figures the HOST creates the authoritative series legends. For differences,
it also creates the zero reference from verified
metadata. Do NOT call label_difference_axis, construct comparison dictionaries, add a legend or add zero lines.
Row categories already name their actual scopes/comparisons when no separate series was planned. Keep the prepared
short quantity/unit label unchanged; full figure titles and explanations belong only in title/caption fields.
The HOST also draws any original-observation overlays, verified tradeoff intervals and a compact statistical footer.
Do not draw extra dots, recompute samples, add footer text, reserve a blank footer strip or alter data/spec.
All visible chart text must be English. Keep the supplied English display labels; do not copy Chinese request,
report or raw column text onto the canvas. Full English figure titles/captions remain external metadata.
''' + (REPAIR_INSTRUCTION if previous else '')
    compact = figure_spec(spec, table)
    for field in ('rc', 'rules', 'series_colors', 'bar_series_colors', 'series_edges', 'series_markers',
                  'series_line_styles', 'series_sizes', 'quantity_semantics'):
        compact['style_contract'].pop(field, None)
    drawing_view = {**table['figure'], 'panels': [{key: value for key, value in panel.items() if key != 'observations'}
                                               for panel in table['figure']['panels']]}
    payload = dict(data={'figure': drawing_view}, spec=compact, native_api=native_interface(reference, spec),
                   execution_rules={'allowed_imports': sorted(ALLOWED_IMPORTS),
                                    'forbidden_names': sorted(FORBIDDEN),
                                    'forbidden_attributes': sorted(FORBIDDEN_ATTRIBUTES)},
                   previous_code=previous, error=error)
    result = call_code_model(prompt + '\nINPUT_JSON:\n' + json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
    code = code_response(result, previous, metadata=('caption',))
    if 'caption' in result:
        if not isinstance(result['caption'], str) or not result['caption'].strip():
            raise ValueError('图注必须为非空文本')
        spec['caption'] = result['caption']
    return code


def prepare_figures(analysis, figures, inputs=None, jobs=None):
    skills = PlotSkills()
    findings = {f['id']: f for f in analysis['findings']}
    prepared = []
    for figure in figures:
        selected = [findings[i] for i in figure['finding_ids']]
        selected_jobs = [job for job in (jobs or []) if job['id'] in figure['finding_ids']]
        columns, rows = finding_table(selected, selected_jobs, inputs)
        bound = dict(columns=columns, rows=rows,
                     metadata={'description': '已验证分析结果；区间语义以 Finding.interval 为准。'})
        spec = {**figure, 'columns': bound['columns'],
                'data_contract': {'group_order': figure['finding_ids'],
                                  'column_indices': {c: i for i, c in enumerate(bound['columns'])}},
                'style_contract': bind_series(skills.style_contract(figure['style'], figure['base_style']),
                                              figure['finding_ids'])}
        spec['style_contract'], context = bind_context(spec['style_contract'], inputs, jobs or selected_jobs)
        spec['style_contract']['quantity_semantics'] = figure_semantics(selected, selected_jobs, analysis['findings'])
        bound['metadata'].update(context)
        view = result_view(selected, selected_jobs, inputs, spec['style_contract'], context)
        bound['metadata'].update(view)
        if view:
            spec['data_contract']['result_view_version'] = 1
            bound['figure'] = prepare_figure_data(figure, bound['metadata'], spec['style_contract'])
            if bound['figure']['panels']:
                spec['data_contract']['prepared_view_version'] = 1
        selected_targets = {finding['target_id'] for finding in selected}
        figure_analysis = {**analysis, 'findings': selected,
                           'summary': '；'.join(finding['statement'] for finding in selected),
                           'targets': [target for target in analysis['targets'] if target['id'] in selected_targets],
                           'diagnostics': []}
        reference = skills.reference(figure['style'], figure['base_style'])
        prepared.append((figure, bound, figure_analysis, spec, reference, dict(skills.hashes)))
    return prepared


def prefetch_key(analysis, figures, inputs, jobs):
    return digest(dict(input_sha256=analysis.get('input_sha256'), figures=figures,
                       inputs=inputs, jobs=jobs))


class PrefetchedCodes:
    """Only first code requests run ahead; no generated program executes here."""

    def __init__(self, analysis, figures, inputs, jobs):
        _, concurrency = limits()
        self.key = prefetch_key(analysis, figures, inputs, jobs)
        self.used = set()
        prepared = prepare_figures(copy.deepcopy(analysis), copy.deepcopy(figures), inputs, copy.deepcopy(jobs))
        self.pool = ThreadPoolExecutor(max_workers=min(concurrency, len(figures)))
        self.futures = {item[0]['id']: self.pool.submit(self.generate, item) for item in prepared}

    @staticmethod
    def generate(item):
        _, bound, analysis, spec, reference, _ = item
        with diagnostics.stage('figure_code_prefetch', figure_id=spec['id'], attempt=1):
            code = generate_code(bound, analysis, spec, reference, '', '')
            name = diagnostics.figure_name(spec['id'])
            diagnostics.save(f'figure-{name}-code-1.py', code)
        return code, spec.get('caption')

    def consume(self, bound, analysis, spec, reference, previous, error):
        identifier = spec['id']
        if identifier in self.used:
            return generate_code(bound, analysis, spec, reference, previous, error)
        self.used.add(identifier)
        code, caption = self.futures[identifier].result()
        if caption:
            spec['caption'] = caption
        return code

    def close(self):
        for future in self.futures.values():
            future.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)


@contextmanager
def prefetch_codes(analysis, figures, inputs=None, jobs=None):
    enabled = os.environ.get('LAZYMIND_PAPER_PLOT_PREFETCH', '1')
    if enabled not in ('0', '1'):
        raise ValueError('绘图代码预生成开关必须为 0 或 1')
    if not figures or enabled == '0':
        yield None
        return
    pending = PrefetchedCodes(analysis, figures, inputs, jobs)
    try:
        yield pending
    finally:
        pending.close()


def draw(analysis, figures, inputs=None, jobs=None, prefetched=None):
    attempts, concurrency = limits()
    prepared = prepare_figures(analysis, figures, inputs, jobs)
    if not prepared:
        return [], []
    generator = generate_code
    if prefetched is not None and prefetched.key == prefetch_key(analysis, figures, inputs, jobs):
        generator = prefetched.consume

    def render_one(item):
        figure, bound, figure_analysis, spec, reference, hashes = item
        if bound.get('figure'):
            diagnostics.save(f"figure-{diagnostics.figure_name(figure['id'])}-data.json", bound['figure'])
        legacy_chart, trace = draw_one(bound, figure_analysis, spec, reference, attempts, code_generator=generator)
        chart = dict(id=figure['id'], title=figure['title'], columns=figure['columns'],
                     svg=legacy_chart['svg'], finding_ids=figure['finding_ids'], caption=spec['caption'])
        trace['exports'] = legacy_chart.get('exports', {})
        trace['skill_resources_sha256'] = hashes
        trace['concurrency'] = min(concurrency, len(prepared))
        trace['first_code_generation_overlapped_statistics'] = generator != generate_code
        return chart, trace

    ordered = [None] * len(prepared)
    # LazyLLM's pool carries request/session ContextVars to each independent figure job.
    with ThreadPoolExecutor(max_workers=min(concurrency, len(prepared))) as pool:
        futures = {pool.submit(render_one, item): index for index, item in enumerate(prepared)}
        try:
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    charts, traces = map(list, zip(*ordered))
    return charts, traces
