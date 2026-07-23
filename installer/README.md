# AM Workbench Installer Skeletons

These scripts are preflight-only packaging skeletons for the hybrid launcher.
They fail closed on relative paths, `..` traversal, staging directories that
resolve outside the repository root, and native builder calls that would
otherwise report success without producing an `.msi`, `.dmg`, or `.AppImage`
artifact. Native installer production is not release-certified until a real
platform builder replaces the fail-closed `build(...)` blockers.
