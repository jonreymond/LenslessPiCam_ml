import os
import hydra
from hydra.utils import to_absolute_path
import subprocess
import numpy as np
import time
from pprint import pprint
from lensless.utils.plot import plot_image, pixel_histogram
from lensless.utils.io import save_image
from lensless.utils.image import resize
import cv2
import matplotlib.pyplot as plt
from lensless import FISTA, ADMM
from lensless.hardware.utils import check_username_hostname, display
from lensless.utils.io import load_image, load_psf


@hydra.main(version_base=None, config_path="../configs", config_name="demo")
def demo(config):

    RPI_USERNAME, RPI_HOSTNAME = check_username_hostname(config.rpi.username, config.rpi.hostname)

    display_fp = to_absolute_path(config.fp)
    if config.save:
        if config.output is not None:
            # make sure output directory exists
            os.makedirs(config.output, exist_ok=True)
            save = config.output
        else:
            save = os.getcwd()
    else:
        save = False

    # 1) Copy file to Raspberry Pi
    print("\nCopying over picture...")
    display(fp=display_fp, rpi_username=RPI_USERNAME, rpi_hostname=RPI_HOSTNAME, **config.display)

    # 2) Take picture
    time.sleep(config.capture.delay)  # for picture to display
    print("\nTaking picture...")

    remote_fn = "remote_capture"
    pic_command = (
        f"{config.rpi.python} {config.capture.script} bayer=True fn={remote_fn} exp={config.capture.exp} iso={config.capture.iso} "
        f"config_pause={config.capture.config_pause} sensor_mode={config.capture.sensor_mode} nbits_out={config.capture.nbits_out}"
    )
    if config.capture.nbits > 8:
        pic_command += " sixteen=True"
    if config.capture.rgb:
        pic_command += " rgb=True"
    if config.capture.legacy:
        pic_command += " legacy=True"
    if config.capture.gray:
        pic_command += " gray=True"
    if config.capture.down:
        pic_command += f" down={config.capture.down}"
    if config.capture.awb_gains:
        pic_command += f" awb_gains=[{config.capture.awb_gains[0]},{config.capture.awb_gains[1]}]"

    print(f"COMMAND : {pic_command}")
    ssh = subprocess.Popen(
        ["ssh", "%s@%s" % (RPI_USERNAME, RPI_HOSTNAME), pic_command],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = ssh.stdout.readlines()
    error = ssh.stderr.readlines()

    if error != []:
        raise ValueError("ERROR: %s" % error)
    if result == []:
        error = ssh.stderr.readlines()
        raise ValueError("ERROR: %s" % error)
    else:
        result = [res.decode("UTF-8") for res in result]
        result = [res for res in result if len(res) > 3]
        result_dict = dict()
        for res in result:
            _key = res.split(":")[0].strip()
            _val = "".join(res.split(":")[1:]).strip()
            result_dict[_key] = _val
        # result_dict = dict(map(lambda s: map(str.strip, s.split(":")), result))
        print("COMMAND OUTPUT : ")
        pprint(result_dict)

    # copy over file
    # more pythonic? https://stackoverflow.com/questions/250283/how-to-scp-in-python
    remotefile = f"~/{remote_fn}.png"
    localfile = f"{config.capture.raw_data_fn}.png"
    print(f"\nCopying over picture as {localfile}...")
    os.system('scp "%s@%s:%s" %s' % (RPI_USERNAME, RPI_HOSTNAME, remotefile, localfile))

    if config.capture.rgb or config.capture.gray:
        img = load_image(localfile, verbose=True)

    else:

        red_gain = config.camera.red_gain
        blue_gain = config.camera.blue_gain

        # get white balance gains
        if red_gain is None:
            red_gain = float(result_dict["Red gain"])
        if blue_gain is None:
            blue_gain = float(result_dict["Blue gain"])

        # load image
        print("\nLoading picture...")
        img = load_image(
            localfile,
            verbose=True,
            bayer=True,
            blue_gain=blue_gain,
            red_gain=red_gain,
            nbits_out=config.capture.nbits_out,
        )

        # write RGB data
        if not config.capture.bayer:
            cv2.imwrite(localfile, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))

    # plot image and histogram (useful to check clipping)
    ax = plot_image(img, gamma=config.capture.gamma)
    ax.set_title("Raw data")
    if save:
        plt.savefig(os.path.join(save, "raw.png"))
    pixel_histogram(img)
    if save:
        plt.savefig(os.path.join(save, "histogram.png"))

    # 3) Reconstruct

    # -- prepare data
    psf, bg = load_psf(
        to_absolute_path(config.camera.psf),
        downsample=config.recon.downsample,
        return_float=True,
        return_bg=True,
        dtype=config.recon.dtype,
    )
    psf = np.array(psf, dtype=config.recon.dtype)
    ax = plot_image(psf[0], gamma=config.recon.gamma)
    ax.set_title("PSF")
    if save:
        plt.savefig(os.path.join(save, "psf.png"))

    data = np.array(img, dtype=config.recon.dtype)
    data -= bg
    data = np.clip(data, a_min=0, a_max=data.max())

    if len(data.shape) == 3:
        data = data[np.newaxis, :, :, :]
    elif len(data.shape) == 2:
        data = data[np.newaxis, :, :, np.newaxis]

    if data.shape != psf.shape:
        # in DiffuserCam dataset, images are already reshaped
        data = resize(data, shape=psf.shape)
    data /= np.linalg.norm(data.ravel())
    data = np.array(data, dtype=config.recon.dtype)

    if config.recon.use_torch:
        import torch

        if config.recon.dtype == "float32":
            torch_dtype = torch.float32
        elif config.recon.dtype == "float64":
            torch_dtype = torch.float64
        else:
            raise ValueError("dtype must be float32 or float64")

        psf = torch.from_numpy(psf).type(torch_dtype).to(config.recon.torch_device)
        data = torch.from_numpy(data).type(torch_dtype).to(config.recon.torch_device)

    # -- apply algo
    start_time = time.time()

    if config.recon.algo == "fista":
        algo_params = config.recon.fista
        recon = FISTA(
            psf,
            **algo_params,
        )
    elif config.recon.algo == "admm":
        algo_params = config.recon.admm
        recon = ADMM(
            psf,
            **algo_params,
        )
    else:
        raise ValueError(f"Unsupported algorithm: {config.recon.algo}")

    recon.set_data(data)
    res = recon.apply(
        gamma=config.recon.gamma,
        save=save,
        plot=config.plot,
        disp_iter=algo_params["disp_iter"],
    )
    print(f"Processing time : {time.time() - start_time} s")

    if config.plot:
        final_image = res[0]
    else:
        final_image = res

    # save final image ax
    if save:

        # take first depth
        final_image = final_image[0]
        if config.recon.use_torch:
            img = final_image.cpu().numpy()
        else:
            img = final_image

        if config.postproc.crop_hor is not None:
            img = img[
                :,
                int(config.postproc.crop_hor[0] * img.shape[1]) : int(
                    config.postproc.crop_hor[1] * img.shape[1]
                ),
            ]
        if config.postproc.crop_vert is not None:
            img = img[
                int(config.postproc.crop_vert[0] * img.shape[0]) : int(
                    config.postproc.crop_vert[1] * img.shape[0]
                ),
                :,
            ]

        output_fp = os.path.join(save, "reconstructed.png")
        save_image(img, output_fp)

    # clean up
    os.remove(localfile)

    if config.plot:
        plt.show()

    return save


if __name__ == "__main__":
    demo()
