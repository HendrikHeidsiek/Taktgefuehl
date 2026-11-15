import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../api.dart';
import '../models.dart';
import '../state/data.dart';
import 'theme.dart';
import 'widgets.dart';

const _months = [
  'Januar',
  'Februar',
  'März',
  'April',
  'Mai',
  'Juni',
  'Juli',
  'August',
  'September',
  'Oktober',
  'November',
  'Dezember',
];

/// "1,24 $", small amounts with a third decimal so a few requests don't all read as 0,00 $.
String fmtUsd(double v) {
  if (v == 0) return '0 \$';
  return '${v.toStringAsFixed(v >= .01 ? 2 : 3).replaceAll('.', ',')} \$';
}

String _fmtPrice(double v) => v.toStringAsFixed(v < 1 ? 2 : (v % 1 == 0 ? 0 : 2)).replaceAll('.', ',');

String fmtTokens(int n) {
  if (n >= 1000000) return '${(n / 1000000).toStringAsFixed(1).replaceAll('.', ',')} Mio.';
  if (n >= 1000) return '${(n / 1000).round()} Tsd.';
  return '$n';
}

String _monthLabel(String ym) {
  final parts = ym.split('-');
  return '${_months[int.parse(parts[1]) - 1]} ${parts[0]}';
}

/// Picks the Claude model the server uses for tagging, checking new songs and naming phases.
class ModelScreen extends StatefulWidget {
  const ModelScreen({super.key});
  @override
  State<ModelScreen> createState() => _ModelScreenState();
}

class _ModelScreenState extends State<ModelScreen> {
  ModelCatalog? _cat;
  String? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _run(() => context.read<Store>().api.aiModels());
  }

  Future<void> _run(Future<ModelCatalog> Function() call) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final cat = await call();
      if (mounted) setState(() => _cat = cat);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _choose(AiModel m) async {
    final store = context.read<Store>();
    await _run(() => store.api.setAiModel(m.id));
    await store.refresh();
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final cat = _cat;
    return Scaffold(
      backgroundColor: c.grouped,
      appBar: AppBar(
        backgroundColor: c.grouped,
        title: const Text('Modell'),
        actions: [
          IconButton(
            tooltip: 'Liste und Preise neu holen',
            icon: const Icon(Icons.refresh),
            onPressed: _busy ? null : () => _run(() => context.read<Store>().api.refreshAiModels()),
          ),
        ],
      ),
      body: cat == null
          ? Center(child: _error == null ? const CircularProgressIndicator() : Text(_error!))
          : ListView(
              padding: const EdgeInsets.only(top: 8, bottom: 24),
              children: [
                if (_busy) const LinearProgressIndicator(minHeight: 2),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.fromLTRB(32, 0, 32, 16),
                    child: Text(_error!, style: context.t.bodyMedium!.copyWith(color: c.danger)),
                  ),
                if (cat.models.isEmpty)
                  const EmptyState(
                    icon: Icons.auto_awesome_outlined,
                    title: 'Keine Modelle',
                    text: 'Der Server konnte die Modellliste noch nicht abrufen. Ist ein Anthropic-Key hinterlegt?',
                  )
                else
                  Section(
                    footer: [
                      'Preise in US-Dollar pro Million Tokens, Eingabe / Ausgabe. Der Server holt Liste und '
                          'Preise alle paar Tage selbst${cat.pricesAt != null ? ', zuletzt ${fmtRelative(cat.pricesAt!)}' : ''}.',
                      if (cat.pricesError != null) cat.pricesError!,
                    ].join(' '),
                    children: [
                      for (final m in cat.models)
                        ListTile(
                          onTap: _busy || m.id == cat.selected ? null : () => _choose(m),
                          leading: Icon(
                            m.id == cat.selected ? Icons.radio_button_checked : Icons.radio_button_off,
                            color: m.id == cat.selected ? c.tint : c.tertiary,
                          ),
                          title: Text(m.name),
                          subtitle: Text(
                            [
                              m.price == null
                                  ? 'Preis unbekannt'
                                  : '${_fmtPrice(m.price!.input)} \$ / ${_fmtPrice(m.price!.output)} \$',
                              if (m.deprecated) 'läuft aus',
                            ].join(' · '),
                          ),
                        ),
                    ],
                  ),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 32),
                  child: Text(
                    'Die Aufgaben sind einfach: Songs einordnen, neue Vorschläge prüfen, Phasen benennen. '
                    'Günstige Modelle reichen oft, die großen kennen aber mehr Nischen-Songs.',
                    style: context.t.bodySmall,
                  ),
                ),
              ],
            ),
    );
  }
}

/// What the AI requests cost: this month, by month, by model and purpose, and the last requests.
class CostsScreen extends StatefulWidget {
  const CostsScreen({super.key});
  @override
  State<CostsScreen> createState() => _CostsScreenState();
}

class _CostsScreenState extends State<CostsScreen> {
  CostSummary? _sum;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final s = await context.read<Store>().api.aiCosts();
      if (mounted) setState(() => _sum = s);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    final s = _sum;
    return Scaffold(
      backgroundColor: c.grouped,
      appBar: AppBar(backgroundColor: c.grouped, title: const Text('KI-Kosten')),
      body: s == null
          ? Center(child: _error == null ? const CircularProgressIndicator() : Text(_error!))
          : RefreshIndicator(onRefresh: _load, child: _body(context, s)),
    );
  }

  Widget _body(BuildContext context, CostSummary s) {
    final c = context.c;
    final now = DateTime.now();
    final thisMonth = '${now.year}-${now.month.toString().padLeft(2, '0')}';
    final current = s.months.where((m) => m.month == thisMonth).firstOrNull;
    if (s.calls == 0) {
      return ListView(
        children: const [
          EmptyState(
            icon: Icons.receipt_long,
            title: 'Noch keine KI-Kosten',
            text: 'Sobald der Server Claude fragt, steht hier, was das gekostet hat.',
          ),
        ],
      );
    }
    return ListView(
      padding: const EdgeInsets.only(top: 8, bottom: 24),
      children: [
        Section(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('DIESER MONAT', style: context.t.labelSmall!.copyWith(letterSpacing: .4)),
                  const SizedBox(height: 4),
                  Text(
                    fmtUsd(current?.cost ?? 0),
                    style: context.t.displaySmall!.copyWith(fontWeight: FontWeight.w700, fontFeatures: tabular),
                  ),
                  if (s.months.length > 1) ...[
                    const SizedBox(height: 12),
                    Sparkline([for (final m in s.months.reversed) m.cost], height: 40),
                  ],
                ],
              ),
            ),
            Row2('Letzte 30 Tage', fmtUsd(s.last30Days)),
            Row2('Insgesamt', fmtUsd(s.total)),
            Row2('Anfragen', '${s.calls}'),
          ],
        ),
        if (current != null) ...[
          _parts(context, 'Wofür, diesen Monat', current.purposes, current.cost),
          _parts(context, 'Modelle, diesen Monat', current.models, current.cost),
        ],
        Section(
          title: 'Monate',
          children: [
            for (final m in s.months)
              ListTile(
                title: Text(_monthLabel(m.month)),
                subtitle: Text(
                  '${m.calls} Anfragen · ${fmtTokens(m.inputTokens)} Tokens ein, ${fmtTokens(m.outputTokens)} aus'
                  '${m.unpriced > 0 ? ' · ${m.unpriced} ohne Preis' : ''}',
                ),
                trailing: Text(fmtUsd(m.cost), style: context.t.bodyLarge!.copyWith(fontFeatures: tabular)),
              ),
          ],
        ),
        Section(
          title: 'Letzte Anfragen',
          footer:
              'Kosten nach den Listenpreisen von Anthropic, ohne Steuern. Die Rechnung steht in der Anthropic-Konsole.',
          children: [
            for (final e in s.recent)
              ListTile(
                dense: true,
                title: Text(e.purpose),
                subtitle: Text(
                  '${fmtRelative(e.at)} · ${e.model} · ${fmtTokens(e.inputTokens)} ein, ${fmtTokens(e.outputTokens)} aus',
                ),
                trailing: Text(
                  e.cost == null ? '–' : fmtUsd(e.cost!),
                  style: context.t.bodyMedium!.copyWith(color: c.secondary, fontFeatures: tabular),
                ),
              ),
          ],
        ),
      ],
    );
  }

  Widget _parts(BuildContext context, String title, List<CostPart> parts, double total) => Section(
    title: title,
    children: [
      for (final p in parts)
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(child: Text(p.name, style: context.t.bodyLarge)),
                  Text(
                    fmtUsd(p.cost),
                    style: context.t.bodyLarge!.copyWith(color: context.c.secondary, fontFeatures: tabular),
                  ),
                ],
              ),
              const SizedBox(height: 6),
              ShareBar(total > 0 ? p.cost / total : 0),
              const SizedBox(height: 4),
              Text('${p.calls} Anfragen', style: context.t.bodySmall),
            ],
          ),
        ),
    ],
  );
}
