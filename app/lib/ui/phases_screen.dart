import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../api.dart';
import '../models.dart';
import '../state/data.dart';
import 'theme.dart';
import 'widgets.dart';

class PhasesScreen extends StatelessWidget {
  const PhasesScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final store = context.watch<Store>();
    final c = context.c;
    return Scaffold(
      backgroundColor: c.grouped,
      body: RefreshIndicator(
        onRefresh: store.refresh,
        child: ListView(padding: const EdgeInsets.only(bottom: 24), children: [
          SafeArea(
            bottom: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(32, 24, 32, 4),
              child: Text('Phasen', style: context.t.displaySmall),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(32, 0, 32, 16),
            child: Text('Gruppen von Songs, die ähnlich klingen. Der Balken zeigt die letzten 26 Wochen.',
                style: context.t.bodyMedium!.copyWith(color: c.secondary)),
          ),
          if (store.phases.isEmpty && store.status != null)
            EmptyState(
              icon: Icons.bubble_chart_outlined,
              title: 'Noch keine Phasen',
              text: 'Sobald YouTube Music verbunden ist, berechnet der Server deine Phasen. '
                  'In der Übersicht kannst du das auch sofort anstoßen.',
            ),
          for (final p in store.phases) _PhaseCard(p),
        ]),
      ),
    );
  }
}

class _PhaseCard extends StatelessWidget {
  final Phase p;
  const _PhaseCard(this.p);

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
      child: Material(
        color: c.card,
        borderRadius: BorderRadius.circular(14),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => PhaseScreen(p))),
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                Expanded(child: Text(p.name, style: context.t.titleLarge)),
                Pill(p.status, strong: p.active),
              ]),
              if (p.description != null) ...[
                const SizedBox(height: 6),
                Text(p.description!, style: context.t.bodyMedium!.copyWith(color: c.secondary)),
              ],
              const SizedBox(height: 12),
              Sparkline(p.weekly),
              const SizedBox(height: 10),
              Row(children: [
                Text('${pct(p.shareTotal)} gesamt', style: context.t.labelMedium),
                if (p.shareRecent != null) Text('  ·  ${pct(p.shareRecent!)} zuletzt', style: context.t.labelMedium),
                const Spacer(),
                Text('${p.songs} Songs', style: context.t.labelMedium),
              ]),
              const SizedBox(height: 6),
              Text(p.artists.take(4).join(', '),
                  maxLines: 1, overflow: TextOverflow.ellipsis, style: context.t.bodySmall),
            ]),
          ),
        ),
      ),
    );
  }
}

class PhaseScreen extends StatelessWidget {
  final Phase phase;
  const PhaseScreen(this.phase, {super.key});

  Future<void> _rename(BuildContext context) async {
    final ctrl = TextEditingController(text: phase.name);
    final name = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Phase umbenennen'),
        content: TextField(controller: ctrl, autofocus: true, maxLength: 60),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Abbrechen')),
          TextButton(onPressed: () => Navigator.pop(ctx, ctrl.text.trim()), child: const Text('Sichern')),
        ],
      ),
    );
    if (name == null || name.isEmpty || !context.mounted) return;
    final store = context.read<Store>();
    try {
      await store.api.renamePhase(phase.id, name);
      await store.refresh();
      if (context.mounted) Navigator.pop(context);
    } on ApiException catch (e) {
      if (context.mounted) showMessage(context, e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return Scaffold(
      backgroundColor: c.grouped,
      appBar: AppBar(
        backgroundColor: c.grouped,
        title: Text(phase.name),
        actions: [IconButton(tooltip: 'Umbenennen', icon: const Icon(Icons.edit), onPressed: () => _rename(context))],
      ),
      body: ListView(padding: const EdgeInsets.only(top: 8, bottom: 24), children: [
        if (phase.description != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(32, 0, 32, 16),
            child: Text(phase.description!, style: context.t.bodyLarge),
          ),
        Section(title: 'Verlauf', children: [
          Padding(padding: const EdgeInsets.all(16), child: Sparkline(phase.weekly, height: 56)),
          Row2('Anteil gesamt', pct(phase.shareTotal)),
          if (phase.shareRecent != null) Row2('Letzte zwei Wochen', pct(phase.shareRecent!)),
          Row2('Status', phase.status),
        ]),
        if (phase.topTags.isNotEmpty)
          Section(title: 'Merkmale', children: [
            Padding(
              padding: const EdgeInsets.all(12),
              child: Wrap(spacing: 6, runSpacing: 6, children: [for (final t in phase.topTags) Pill(t)]),
            ),
          ]),
        Section(title: 'Meistgehört', children: [
          for (final s in phase.topSongs)
            ListTile(
              title: Text(s.title, maxLines: 1, overflow: TextOverflow.ellipsis),
              subtitle: Text(s.artist ?? ''),
              trailing: Icon(Icons.open_in_new, size: 18, color: c.tertiary),
              onTap: () => launchUrl(s.url, mode: LaunchMode.externalApplication),
            ),
        ]),
      ]),
    );
  }
}
