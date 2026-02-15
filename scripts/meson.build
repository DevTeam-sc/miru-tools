scripts = [
  ['miru', 'repl'],
  ['miru-apk', 'apk'],
  ['miru-compile', 'compiler'],
  ['miru-create', 'create'],
  ['miru-discover', 'discover'],
  ['miru-itrace', 'itracer'],
  ['miru-join', 'join'],
  ['miru-kill', 'kill'],
  ['miru-ls', 'ls'],
  ['miru-ls-devices', 'lsd'],
  ['miru-pm', 'pm'],
  ['miru-ps', 'ps'],
  ['miru-pull', 'pull'],
  ['miru-push', 'push'],
  ['miru-rm', 'rm'],
  ['miru-trace', 'tracer'],
]

common_cdata = configuration_data()
common_cdata.set('PYTHON', python.full_path())
common_cdata.set('pythondir', python.get_install_dir())

foreach s : scripts
  cdata = configuration_data()
  cdata.merge_from(common_cdata)
  cdata.set('module', s.get(1))

  generated_script = configure_file(
    input: 'script.in',
    output: s.get(0),
    configuration: cdata,
  )

  install_data(generated_script,
    install_dir: get_option('bindir'),
    install_mode: 'rwxr-xr-x',
  )
endforeach
