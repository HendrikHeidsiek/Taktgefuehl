import 'package:flutter/material.dart';

import 'theme.dart';

/// Grouped card like iOS settings sections, with an optional header above.
class Section extends StatelessWidget {
  final String? title;
  final String? footer;
  final List<Widget> children;
  final EdgeInsets padding;
  const Section({super.key, this.title, this.footer, required this.children, this.padding = EdgeInsets.zero});

  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (title != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: Text(title!.toUpperCase(), style: context.t.labelSmall!.copyWith(letterSpacing: .4)),
          ),
        Container(
          decoration: BoxDecoration(color: c.card, borderRadius: BorderRadius.circular(14)),
          clipBehavior: Clip.antiAlias,
          padding: padding,
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            for (var i = 0; i < children.length; i++) ...[
              if (i > 0) const Divider(indent: 16),
              children[i],
            ],
          ]),
        ),
        if (footer != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            child: Text(footer!, style: context.t.bodySmall),
          ),
      ]),
    );
  }
}

class Row2 extends StatelessWidget {
  final String label;
  final String value;
  final Color? valueColor;
  const Row2(this.label, this.value, {super.key, this.valueColor});
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(children: [
          Expanded(child: Text(label, style: context.t.bodyLarge)),
          Text(value,
              style: context.t.bodyLarge!.copyWith(
                  color: valueColor ?? context.c.secondary, fontFeatures: tabular)),
        ]),
      );
}

/// Small rounded label, e.g. "aktive Phase".
class Pill extends StatelessWidget {
  final String text;
  final bool strong;
  const Pill(this.text, {super.key, this.strong = false});
  @override
  Widget build(BuildContext context) {
    final c = context.c;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: strong ? c.tint.withValues(alpha: .18) : c.fill,
        borderRadius: BorderRadius.circular(99),
      ),
      child: Text(text,
          style: context.t.labelSmall!.copyWith(color: strong ? c.tintText : c.secondary, fontWeight: FontWeight.w600)),
    );
  }
}

/// Weekly listening share as bars; null weeks (no data) stay empty.
class Sparkline extends StatelessWidget {
  final List<double?> values;
  final double height;
  const Sparkline(this.values, {super.key, this.height = 28});
  @override
  Widget build(BuildContext context) =>
      SizedBox(height: height, child: CustomPaint(painter: _SparkPainter(values, context.c), size: Size.infinite));
}

class _SparkPainter extends CustomPainter {
  final List<double?> values;
  final AppColors c;
  _SparkPainter(this.values, this.c);

  @override
  void paint(Canvas canvas, Size size) {
    if (values.isEmpty) return;
    final top = values.whereType<double>().fold<double>(0, (a, b) => b > a ? b : a);
    final w = size.width / values.length;
    final empty = Paint()..color = c.fill;
    final bar = Paint()..color = c.tint;
    for (var i = 0; i < values.length; i++) {
      final v = values[i];
      final x = i * w + w * .15;
      final r = RRect.fromRectAndRadius(Rect.fromLTWH(x, size.height - 2, w * .7, 2), const Radius.circular(1));
      canvas.drawRRect(r, empty);
      if (v == null || top == 0) continue;
      final h = (v / top * size.height).clamp(2.0, size.height);
      canvas.drawRRect(
          RRect.fromRectAndRadius(Rect.fromLTWH(x, size.height - h, w * .7, h), const Radius.circular(2)), bar);
    }
  }

  @override
  bool shouldRepaint(_SparkPainter old) => old.values != values || old.c != c;
}

/// Horizontal share bar.
class ShareBar extends StatelessWidget {
  final double share;
  const ShareBar(this.share, {super.key});
  @override
  Widget build(BuildContext context) => ClipRRect(
        borderRadius: BorderRadius.circular(3),
        child: LinearProgressIndicator(value: share.clamp(0, 1), minHeight: 6),
      );
}

void showMessage(BuildContext context, String text) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

class EmptyState extends StatelessWidget {
  final IconData icon;
  final String title;
  final String text;
  final Widget? action;
  const EmptyState({super.key, required this.icon, required this.title, required this.text, this.action});
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(32, 64, 32, 32),
        child: Column(children: [
          Icon(icon, size: 44, color: context.c.tertiary),
          const SizedBox(height: 16),
          Text(title, style: context.t.titleMedium, textAlign: TextAlign.center),
          const SizedBox(height: 8),
          Text(text, style: context.t.bodyMedium!.copyWith(color: context.c.secondary), textAlign: TextAlign.center),
          if (action != null) ...[const SizedBox(height: 20), action!],
        ]),
      );
}
