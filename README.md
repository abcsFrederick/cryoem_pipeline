# CryoEM Pipeline

This project integrates data management functions with a CryoEM quality control and preprocessing package.

## Getting Started

### Prequisittes

Python 3.5 or higher is required. The application uses features from the asyncio module that were not introduced until 3.5. It has not been tested with 3.7, but should work.

The globus command line tools must be installed for the globus data transfer functions to work. These will not impede the rest of the operations, but you will get logging errors without them. If just compressing to a local drive and handling web-transfer separately.

By default, lbzip2 and IMOD are also required. The functions that use them could be patched out pretty reasonably (replacing lbzip2 with another parallel compression application or replacing the few IMOD commands that are used with something else).

The wrapper assumes that it is supporting an existing Scipion installation.  In the future, this will be a more flexible function but for now is a fairly rigid requirement. The assumptions are that

- Scipion is installed and running
- The html status monitoring page is in use

This application also requires the ```transitions``` python module (available on pypi), which in turn requires ```six```. Do the following after cloning to install the required module with the standard
```shell
pip install -r requirements.txt
```
**Note:** You are encouraged to run in a virtual environment or separate installation, but that configuration is left up to the user.

### Installation

Setup is currently a very manual process, requiring multiple hardcoded options to be changed to fit the target system. All of this is planned to move to a config file that make much more sense; once that happens this section will be fleshed out more.