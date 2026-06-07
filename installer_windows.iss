#define AppName      "Transcript Agent"
#define AppPublisher "Coastline6"
#define AppURL       "https://github.com/jayuan101/transcript-agent"
#ifndef AppVersion
  #define AppVersion "2.4.2"
#endif

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={localappdata}\TranscriptAgent
DisableDirPage=yes
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=TranscriptAgent-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
CloseApplications=yes
UninstallDisplayIcon={app}\TranscriptAgent\TranscriptAgent.exe
; Supports x64 and Windows ARM64 (runs via x64 emulation on ARM)
ArchitecturesAllowed=x64compatible arm64
ArchitecturesInstallIn64BitMode=x64compatible arm64
MinVersion=10.0.17763
SetupIconFile=icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checked

[Files]
; App bundle (PyInstaller onedir output)
Source: "dist\TranscriptAgent\*"; DestDir: "{app}\TranscriptAgent"; Flags: ignoreversion recursesubdirs createallsubdirs
; OTA launcher files
Source: "dist\ta_launcher.ps1";            DestDir: "{app}"; Flags: ignoreversion
Source: "dist\Launch-TranscriptAgent.bat"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\{#AppName}"; \
  Filename: "{app}\Launch-TranscriptAgent.bat"; \
  WorkingDir: "{app}"; \
  IconFilename: "{app}\TranscriptAgent\TranscriptAgent.exe"; \
  Comment: "Transcript Agent — AI transcription and interview analysis"; \
  Tasks: desktopicon
Name: "{group}\{#AppName}"; \
  Filename: "{app}\Launch-TranscriptAgent.bat"; \
  WorkingDir: "{app}"; \
  IconFilename: "{app}\TranscriptAgent\TranscriptAgent.exe"

[UninstallDelete]
Type: files; Name: "{app}\version.txt"
Type: files; Name: "{app}\ta_launcher.ps1"
Type: files; Name: "{app}\Launch-TranscriptAgent.bat"

[Run]
Filename: "{app}\Launch-TranscriptAgent.bat"; \
  Description: "Launch {#AppName} now"; \
  Flags: postinstall shellexec skipifsilent nowait

[Messages]
WelcomeLabel1=Welcome to {#AppName} Setup
WelcomeLabel2=This will install Transcript Agent {#AppVersion} on your computer.%n%nThe app opens in your browser at http://localhost:7860.%n%nUpdates are automatic — the app checks for new versions each time you launch it.%n%nClick Next to continue.
FinishedLabel=Transcript Agent has been installed.%n%nClick Finish to launch it now.

[Code]
{ Write version.txt and detect GPU on install }
procedure CurStepChanged(CurStep: TSetupStep);
var
  VersionFile: string;
  WbemLocator, WbemServices, WbemObjectSet, WbemObject: Variant;
  GpuName, GpuHint: string;
  i: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    { Write version.txt }
    VersionFile := ExpandConstant('{app}\version.txt');
    SaveStringToFile(VersionFile, '{#AppVersion}', False);

    { GPU detection — store hint for the app }
    GpuHint := 'cpu';
    try
      WbemLocator  := CreateOleObject('WbemScripting.SWbemLocator');
      WbemServices := WbemLocator.ConnectServer('.', 'root\cimv2');
      WbemObjectSet := WbemServices.ExecQuery('SELECT Name FROM Win32_VideoController');
      for i := 0 to WbemObjectSet.Count - 1 do
      begin
        WbemObject := WbemObjectSet.ItemIndex(i);
        GpuName := WbemObject.Name;
        if (Pos('NVIDIA', UpperCase(GpuName)) > 0) then
          GpuHint := 'cuda'
        else if (Pos('AMD', UpperCase(GpuName)) > 0) or (Pos('RADEON', UpperCase(GpuName)) > 0) then
          GpuHint := 'directml';
      end;
    except
    end;
    SaveStringToFile(ExpandConstant('{app}\gpu_hint.txt'), GpuHint, False);
  end;
end;
