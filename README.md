## Development

1. Package the plugin for OpenRV:

```bash
./package.sh
```

2. Install the package in OpenRV > Preferences > Packages > Add Packages or use the OpenRV CLI:

```sh
OPENRV_PATH/_build/stage/app/bin/rvpkg -uninstall $HOME/.rv/Packages/kitsu-1.0.rvpkg -force
OPENRV_PATH/_build/stage/app/bin/rvpkg -remove $HOME/.rv/Packages/kitsu-1.0.rvpkg -force
OPENRV_PATH/_build/stage/app/bin/rvpkg -add $HOME/.rv/Packages PACKAGE_PATH/kitsu-1.0.rvpkg 
OPENRV_PATH/_build/stage/app/bin/rvpkg -install $HOME/.rv/Packages/kitsu-1.0.rvpkg
```

## TODO

- bug fix: incorrect parsing - some properties are parsed as arrays instead of scalars