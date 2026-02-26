                    if sig is None:
                        continue
                    if sig.direction != dir_label:
                        continue
                    if sig.quality < user.min_quality:
                        continue

                    max_sig_risk = getattr(user, "max_signal_risk_pct", 0)
                    if max_sig_risk > 0 and getattr(sig, "risk_pct", 0) > max_sig_risk:
                        continue

                    # 🔹 Фильтр по min_risk_level (новое поле в UserSettings)
                    rl = _risk_level(sig.quality)
                    if user.min_risk_level == "low":
                        # Берём только low
                        if rl != "low":
                            continue
                    elif user.min_risk_level == "medium":
                        # Берём medium и low
                        if rl not in ("low", "medium"):
                            continue
                    # "all" — пропускаем без доп. фильтра

                    if user.notify_signal:
                        await self._send_signal(user, sig)
                    signals += 1
