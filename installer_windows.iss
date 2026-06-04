#define AppName "Transcript Agent"
#define AppVersion "1.1.82"
#define AppPublisher "Coastline6"
#define AppURL "https://huggingface.co/spaces/Coastline6/transcript-agent-v2"
#define AppExeName "run.bat"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={localappdata}\TranscriptAgent
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir=dist
OutputBaseFilename=TranscriptAgent-Windows-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\run.bat
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "app.py";              DestDir: "{app}"; Flags: ignoreversion
Source: "transcript_agent.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "requirements.txt";    DestDir: "{app}"; Flags: ignoreversion
Source: "setup_windows.bat";   DestDir: "{app}"; Flags: ignoreversion
Source: "run.bat";             DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.py";         DestDir: "{app}"; Flags: ignoreversion
Source: "CHANGELOG.md";        DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\setup_windows.bat"; WorkingDir: "{app}"; Tasks: desktopicon; Comment: "Launch Transcript Agent"

[Run]
Filename: "{app}\setup_windows.bat"; WorkingDir: "{app}"; Description: "Run setup (installs Python dependencies)"; Flags: postinstall shellexec skipifsilent

[Messages]
WelcomeLabel1=Welcome to Transcript Agent v{#AppVersion} Setup
WelcomeLabel2=This will install Transcript Agent on your computer.%n%nOn first launch, setup will download Python dependencies (~2 GB). Make sure you have a working internet connection.%n%nClick Next to continue.
