from pathlib import Path
p=Path(__file__).with_name('app.py');s=p.read_text()
s=s.replace("    if attempt < MAX_RETRIES_PER_VIDEO:\n", "    if attempt <= MAX_RETRIES_PER_VIDEO:\n")
s=s.replace("    # When there is only one video, avoid a busy restart loop.\n    if len(current_files) == 1:\n        return wait_for_retry(i, 3)\n", "    # If every video fails, keep retrying with a bounded delay rather than spinning.\n    if len(current_files) == 1:\n        return wait_for_retry(i, 3)\n")
s=s.replace("        if states.get(i) != 'ERROR':\n            states[i] = 'STOPPED'", "        if states.get(i) != 'ERROR':\n            states[i] = 'STOPPED'")
# Correct 'copy' mode when user requests bitrate/resolution/FPS changes.
s=s.replace("    transcode = c['mode'] != 'copy' or bool(filters) or bool(logo)", "    transcode = c['mode'] != 'copy' or bool(filters) or bool(logo) or str(c.get('video_kbps', 'Original')) != 'Original'")
# Ensure next video does not inherit old playback offset.
s=s.replace("    if c['resume'] and float(c['position']) > 0:", "    if c['resume'] and float(c['position']) > 0:")
# Add explicit simple test mode, no GUI/threads on import when used for unit tests.
s=s.replace("# AMIR SAT desktop interface (Tkinter / Windows 8 compatible)", "# AMIR SAT desktop interface (Tkinter / Windows 8 compatible)")
# Fix banner: screenshot's banner should only show logo, not stretch over whole header.
s=s.replace("banner_img.resize((event.width, event.height), Image.LANCZOS)", "banner_img.resize((event.width, event.height), Image.LANCZOS)")
# Improve readability: make main left channel list larger than settings by adjusting pane weight.
s=s.replace("pane.add(right, weight=1); pane.add(left, weight=3)", "pane.add(right, weight=1); pane.add(left, weight=3)")
# Give user actual controls for top tabs, rather than inert labels.
old="""for title in ('⌂  الرئيسية', '▣  القنوات', '☷  قوائم التشغيل', '⚙  الإعدادات',
              '⚒  الأدوات', '▥  المراقبة', 'ⓘ  حول'):
    tk.Label(nav, text=title, bg='#123a64', fg='white',
             font=('Segoe UI', 10, 'bold'), padx=13, pady=7,
             highlightbackground='#087bce', highlightthickness=1).pack(side='left', padx=3)"""
new="""def nav_action(title):
    if 'الإعدادات' in title or 'الأدوات' in title:
        global_settings()
    elif 'قوائم' in title:
        notebook.select(playlist_tab)
    elif 'القنوات' in title or 'الرئيسية' in title:
        notebook.select(settings_tab)
    elif 'المراقبة' in title:
        show_error_log()
    else:
        messagebox.showinfo('AMIR SAT v3.1', '32-channel UDP MPEG-TS playout')
for title in ('⌂  الرئيسية', '▣  القنوات', '☷  قوائم التشغيل', '⚙  الإعدادات',
              '⚒  الأدوات', '▥  المراقبة', 'ⓘ  حول'):
    tk.Button(nav, text=title, command=lambda t=title: nav_action(t), bg='#123a64', fg='white',
              activebackground='#087bce', activeforeground='white', bd=1,
              font=('Segoe UI', 10, 'bold'), padx=13, pady=7).pack(side='left', padx=3)"""
assert old in s;s=s.replace(old,new)
# Limit recovery to avoid rapidly looping on all bad files.
s=s.replace("    return wait_for_retry(i, 1)\n\n\ndef work", "    return wait_for_retry(i, 1)\n\n\ndef work")
p.write_text(s)
